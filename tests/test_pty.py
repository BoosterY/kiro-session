"""PTY-based integration tests for kiro-session.

Spawns kiro-session / kiro-cli in a PTY, injects keystrokes, checks output.
Run: python3 tests/test_pty.py
"""
import json
import os
import pty
import re
import select
import signal
import sqlite3
import struct
import fcntl
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
VENV_PYTHON = Path.home() / ".kiro/skills/session-manager/.venv/bin/python3"
KIRO_SESSION = str(Path.home() / ".local/bin/kiro-session")
DB_PATH = Path.home() / ".local/share/kiro-cli/data.sqlite3"
INDEX_DB = Path.home() / ".kiro/session-index.db"
TMP_DIR = Path.home() / ".kiro/tmp"

ANSI_RE = re.compile(rb'\x1b\[[0-9;]*m')


def _strip_ansi(data: bytes) -> str:
    return ANSI_RE.sub(b'', data).decode('utf-8', errors='replace')


def _pty_run(cmd: list[str], inject: bytes | None = None, timeout: float = 15,
             wait_for: bytes | None = None, cwd: str = "/tmp") -> str:
    """Spawn cmd in PTY, optionally inject input, return cleaned output."""
    prompt_re = re.compile(rb'(?:^|\n|\r)(?:\x1b\[[0-9;]*m)*>\s', re.MULTILINE)

    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvp(cmd[0], cmd)
        os._exit(1)

    ws = struct.pack('HHHH', 24, 120, 0, 0)
    fcntl.ioctl(master_fd, 0x5414, ws)

    buf = b""
    injected = inject is None
    deadline = time.monotonic() + timeout
    output = b""

    while time.monotonic() < deadline:
        try:
            r, _, _ = select.select([master_fd], [], [], 0.1)
        except (select.error, ValueError, OSError):
            break
        if master_fd in r:
            try:
                data = os.read(master_fd, 8192)
                if not data:
                    break
                output += data
                if not injected:
                    buf += data
            except OSError:
                break

        if not injected and (prompt_re.search(buf) or time.monotonic() >= deadline - 5):
            os.write(master_fd, inject)
            injected = True
            deadline = time.monotonic() + timeout

        if wait_for and wait_for in output:
            time.sleep(0.5)
            try:
                r, _, _ = select.select([master_fd], [], [], 0.3)
                if r:
                    output += os.read(master_fd, 8192)
            except:
                pass
            break

    try:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
    except:
        pass
    try:
        os.close(master_fd)
    except:
        pass

    return _strip_ansi(output)


def _cleanup_sqlite(*patterns):
    conn = sqlite3.connect(str(DB_PATH))
    for p in patterns:
        conn.execute(f"DELETE FROM conversations_v2 WHERE conversation_id LIKE '{p}%'")
    conn.execute("DELETE FROM conversations_v2 WHERE key = '/tmp'")
    conn.commit()
    conn.close()
    # Also clean JSONL sessions created in /tmp
    sessions_dir = Path.home() / ".kiro/sessions/cli"
    if sessions_dir.exists():
        for f in sessions_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    meta = json.load(fh)
                if meta.get("cwd") == "/tmp":
                    f.unlink()
                    jsonl = f.with_suffix(".jsonl")
                    if jsonl.exists():
                        jsonl.unlink()
            except Exception:
                pass


def _get_jsonl_session_id():
    """Find a JSONL-only session for testing."""
    sessions_dir = Path.home() / ".kiro/sessions/cli"
    if not sessions_dir.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    sqlite_ids = {r[0] for r in conn.execute("SELECT conversation_id FROM conversations_v2").fetchall()}
    conn.close()
    for f in sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_size):
        sid = f.stem
        if sid not in sqlite_ids and f.stat().st_size > 1000:
            return sid
    return None


def _get_indexed_session():
    """Find a session with topics in the index."""
    conn = sqlite3.connect(str(INDEX_DB))
    row = conn.execute(
        "SELECT s.id FROM sessions s JOIN topics t ON s.id = t.session_id "
        "WHERE s.llm_enriched = 1 GROUP BY s.id HAVING COUNT(*) >= 2 LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


class TestListPlain(unittest.TestCase):
    def test_list_plain_output(self):
        r = subprocess.run([KIRO_SESSION, "list", "--plain"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("ago", r.stdout)
        lines = [l for l in r.stdout.strip().split('\n') if l.strip()]
        self.assertGreater(len(lines), 0, "Should list at least one session")


class TestResumeJsonFormat(unittest.TestCase):
    """Test that resume generates valid JSON that kiro-cli /chat load accepts."""

    def setUp(self):
        self.sid = _get_jsonl_session_id()
        if not self.sid:
            self.skipTest("No JSONL-only session available")
        _cleanup_sqlite(self.sid[:8])

    def tearDown(self):
        if hasattr(self, 'sid') and self.sid:
            _cleanup_sqlite(self.sid[:8])
        for f in TMP_DIR.glob("test-*.json"):
            f.unlink(missing_ok=True)

    def test_resume_json_structure(self):
        """Generated resume JSON has correct ConversationState structure."""
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        import extractor
        importlib.reload(extractor)
        extractor._TEMPLATE_CACHE = None

        data = extractor.read_session_data(self.sid)
        self.assertIsNotNone(data)

        # Must have history with ConversationState entries
        history = data.get("history", [])
        self.assertGreater(len(history), 0)

        for i, h in enumerate(history):
            self.assertIn("user", h, f"h[{i}] missing 'user'")
            self.assertIn("assistant", h, f"h[{i}] missing 'assistant'")
            self.assertIn("request_metadata", h, f"h[{i}] missing 'request_metadata'")

            user = h["user"]
            self.assertIn("content", user)
            content = user["content"]
            self.assertTrue(
                "Prompt" in content or "ToolUseResults" in content,
                f"h[{i}] unexpected content type: {list(content.keys())}"
            )

            # timestamp: str or None, never empty string
            ts = user.get("timestamp")
            self.assertTrue(ts is None or (isinstance(ts, str) and len(ts) > 0),
                            f"h[{i}] bad timestamp: {repr(ts)}")

            assistant = h["assistant"]
            self.assertTrue(
                "Response" in assistant or "ToolUse" in assistant,
                f"h[{i}] unexpected assistant type: {list(assistant.keys())}"
            )

            # ToolUseResults content must use Text variant only
            if "ToolUseResults" in content:
                for r in content["ToolUseResults"]["tool_use_results"]:
                    for c in r["content"]:
                        self.assertIn("Text", c,
                                      f"h[{i}] tool result must use Text, got {list(c.keys())}")

    def test_resume_chat_load_pty(self):
        """Generated resume JSON can be loaded by kiro-cli /chat load in PTY."""
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        import extractor
        importlib.reload(extractor)
        extractor._TEMPLATE_CACHE = None

        data = extractor.read_session_data(self.sid)
        # Use first 5 turns to keep test fast
        data["history"] = data["history"][:5]
        data["valid_history_range"] = [0, 5]

        test_file = TMP_DIR / "test-pty-load.json"
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        with open(test_file, "w") as f:
            json.dump(data, f, ensure_ascii=False)

        output = _pty_run(
            ["kiro-cli", "chat"],
            inject=f"/chat load {test_file}\r".encode(),
            timeout=20,
            wait_for=b"Imported",
        )

        _cleanup_sqlite(self.sid[:8])
        test_file.unlink(missing_ok=True)

        self.assertIn("Imported", output, f"chat load failed. Output: {output[-300:]}")


class TestTopicResumeFormat(unittest.TestCase):
    """Test that topic resume generates valid JSON."""

    def setUp(self):
        self.sid = _get_indexed_session()
        if not self.sid:
            self.skipTest("No indexed session with topics available")

    def tearDown(self):
        for f in TMP_DIR.glob(f"{self.sid[:8]}-topic-*.json"):
            f.unlink(missing_ok=True)

    def test_topic_file_structure(self):
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        import splitter
        importlib.reload(splitter)
        import index_store as idx

        conn = idx.connect()
        result = splitter.generate_topic_file(conn, self.sid, 0)
        conn.close()

        self.assertIsNotNone(result, "generate_topic_file returned None")
        path = result

        with open(path) as f:
            data = json.load(f)

        self.assertIn("history", data)
        self.assertGreater(len(data["history"]), 0)

        for h in data["history"]:
            self.assertIn("user", h)
            self.assertIn("assistant", h)
            ts = h["user"].get("timestamp")
            self.assertTrue(ts is None or (isinstance(ts, str) and len(ts) > 0))


class TestSaveExportSearch(unittest.TestCase):
    def test_save(self):
        r = subprocess.run([KIRO_SESSION, "list", "--plain"],
                           capture_output=True, text=True, timeout=30)
        # Extract session ID from lines like "   1. 771f40ff  ..."
        sid = None
        for line in r.stdout.split('\n'):
            m = re.search(r'\b([0-9a-f]{8})\b', line)
            if m:
                sid = m.group(1)
                break
        if not sid:
            self.skipTest("Cannot parse session ID")

        out = Path("/tmp/test-save-output.json")
        r = subprocess.run([KIRO_SESSION, "save", sid, str(out)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, f"save failed: {r.stderr}")
        self.assertTrue(out.exists(), f"Output file not created. stdout: {r.stdout}")

        with open(out) as f:
            data = json.load(f)
        self.assertIn("history", data)
        out.unlink()

    def test_search(self):
        r = subprocess.run([KIRO_SESSION, "search", "test"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_export(self):
        r = subprocess.run([KIRO_SESSION, "list", "--plain"],
                           capture_output=True, text=True, timeout=30)
        sid = None
        for line in r.stdout.split('\n'):
            m = re.search(r'\b([0-9a-f]{8})\b', line)
            if m:
                sid = m.group(1)
                break
        if not sid:
            self.skipTest("Cannot parse session ID")

        r = subprocess.run([KIRO_SESSION, "export", sid, "--dir", "/tmp"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        # Clean up exported file
        for f in Path("/tmp").glob("session-*.md"):
            f.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)

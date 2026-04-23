"""Launch kiro-cli with --resume-picker, auto-selecting the target session via PTY."""
import json
import os
import pty
import select
import signal
import sqlite3
import sys
import fcntl
import termios
import tty
import time
from datetime import datetime
from pathlib import Path


def _get_picker_index(session_id: str, cwd: str) -> int:
    """Find the target session's position in the picker list (sorted by updated_at DESC)."""
    sessions = []

    # v1: SQLite (updated_at is int ms)
    db_path = Path.home() / ".local/share/kiro-cli/data.sqlite3"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        for row in conn.execute(
            "SELECT conversation_id, updated_at FROM conversations_v2 WHERE key = ?", (cwd,)
        ).fetchall():
            sessions.append((row[1] / 1000, row[0]))  # (epoch_sec, cid)
        conn.close()

    # v2: JSONL metadata (updated_at is ISO string, always UTC with Z suffix)
    jsonl_dir = Path.home() / ".kiro/sessions/cli"
    if jsonl_dir.exists():
        seen = {s[1] for s in sessions}
        for f in jsonl_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    meta = json.load(fh)
            except Exception:
                continue
            sid = meta.get("session_id", "")
            if sid in seen or meta.get("cwd", "") != cwd:
                continue
            ts_str = meta.get("updated_at", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                ts = 0
            sessions.append((ts, sid))

    # Sort by updated_at descending (matches picker order)
    sessions.sort(key=lambda x: x[0], reverse=True)
    for i, (_, cid) in enumerate(sessions):
        if cid.startswith(session_id) or session_id.startswith(cid[:8]):
            return i
    return 0


def _write_to_kiro_db(cwd: str, conversation_id: str, data: dict):
    """Write a session into kiro-cli's SQLite DB."""
    db_path = Path.home() / ".local/share/kiro-cli/data.sqlite3"
    if not db_path.exists():
        return
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO conversations_v2 "
            "(key, conversation_id, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cwd, conversation_id, json.dumps(data, ensure_ascii=False), now_ms, now_ms),
        )
        conn.commit()
    finally:
        conn.close()


def _touch_session_in_db(cwd: str, session_id: str):
    """Update updated_at to now so the session sorts first in picker."""
    now_ms = int(time.time() * 1000)

    # v1: SQLite
    db_path = Path.home() / ".local/share/kiro-cli/data.sqlite3"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE conversations_v2 SET updated_at = ? WHERE key = ? AND conversation_id = ?",
                (now_ms, cwd, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    # v2: JSONL meta
    meta_path = Path.home() / ".kiro/sessions/cli" / f"{session_id}.json"
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            meta["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
            with open(meta_path, "w") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception:
            pass


def launch_kiro_resume(cwd: str, session_id: str, trust_tools: str = "",
                       ui_mode: str = "", touched: bool = False):
    """Launch kiro-cli --resume-picker, auto-select the target session."""
    if not sys.stdin.isatty():
        return False

    picker_index = 0 if touched else _get_picker_index(session_id, cwd)

    cmd = ["kiro-cli", "chat", "--resume-picker"]
    if ui_mode == "tui":
        cmd.append("--tui")
    elif ui_mode == "legacy":
        cmd.append("--legacy-ui")
    if trust_tools:
        cmd.append(f"--trust-tools={trust_tools}")

    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvp(cmd[0], cmd)
        sys.exit(1)

    def _sync_winsize():
        try:
            ws = fcntl.ioctl(sys.stdin, termios.TIOCGWINSZ, b'\x00' * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
            os.kill(pid, signal.SIGWINCH)
        except Exception:
            pass

    _sync_winsize()
    signal.signal(signal.SIGWINCH, lambda *_: _sync_winsize())

    old = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin)
        selected = False
        deadline = time.monotonic() + 15
        buf = b""

        while True:
            try:
                r, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.05)
            except (select.error, ValueError, OSError):
                break

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                    if not selected:
                        buf += data
                        if len(buf) > 16384:
                            buf = buf[-8192:]
                except OSError:
                    break

            if not selected:
                if b"Select a chat session" in buf:
                    time.sleep(0.3)
                    for _ in range(picker_index):
                        os.write(master_fd, b"\x1b[B")
                        time.sleep(0.05)
                    os.write(master_fd, b"\r")
                    selected = True
                    buf = b""
                elif time.monotonic() >= deadline:
                    # Single session — picker auto-selected, no interaction needed
                    selected = True
                    buf = b""

            if sys.stdin.fileno() in r:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    os.write(master_fd, data)
                except OSError:
                    break
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            status = 0
    sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)




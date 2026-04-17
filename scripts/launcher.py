"""Launch kiro-cli with auto-injected /chat load command."""
import os
import pty
import re
import select
import signal
import sys
import fcntl
import termios
import tty
import time


# Matches kiro-cli's ready prompt: ">" possibly preceded by ANSI escapes, at line start
_PROMPT_RE = re.compile(rb'(?:^|\n|\r)(?:\x1b\[[0-9;]*m)*>\s', re.MULTILINE)

_READY_TIMEOUT = 10  # seconds


def launch_kiro_resume(cwd: str, load_path: str, trust_tools: str = ""):
    """Spawn kiro-cli chat in a PTY, inject /chat load when ready, hand off to user."""
    if not sys.stdin.isatty():
        return False

    cmd = ["kiro-cli", "chat"]
    if trust_tools:
        cmd.append(f"--trust-tools={trust_tools}")

    inject_cmd = f"/chat load {load_path}\r".encode()

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
        injected = False
        deadline = time.monotonic() + _READY_TIMEOUT
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
                    if not injected:
                        buf += data
                        # Keep only tail to bound memory
                        if len(buf) > 8192:
                            buf = buf[-4096:]
                except OSError:
                    break

            if not injected:
                ready = _PROMPT_RE.search(buf) or time.monotonic() >= deadline
                if ready:
                    os.write(master_fd, inject_cmd)
                    injected = True
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

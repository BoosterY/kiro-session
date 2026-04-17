"""Launch kiro-cli with auto-injected /chat load command."""
import os
import pty
import select
import signal
import sys
import fcntl
import termios
import struct
import tty
import time


def launch_kiro_resume(cwd: str, load_path: str, trust_tools: str = "", delay: float = 2.5):
    """Spawn kiro-cli chat in a PTY, inject /chat load after delay, hand off to user."""
    if not sys.stdin.isatty():
        print("Error: --go requires an interactive terminal.", file=sys.stderr)
        sys.exit(1)

    cmd = ["kiro-cli", "chat"]
    if trust_tools:
        cmd.append(f"--trust-tools={trust_tools}")

    inject_cmd = f"/chat load {load_path}\r".encode()

    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvp(cmd[0], cmd)
        sys.exit(1)

    # Propagate terminal size
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
        inject_time = time.monotonic() + delay
        injected = False

        while True:
            try:
                r, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.05)
            except (select.error, ValueError, OSError):
                break

            if not injected and time.monotonic() >= inject_time:
                os.write(master_fd, inject_cmd)
                injected = True

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                except OSError:
                    break

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

"""
Start Cartel, wait until it is genuinely answering, then open the browser.

Written because the previous arrangement asked too much of the user: start one
window, keep it open, start a second window to check it. If the first window
gets closed the second reports "nothing is listening", which is true but says
nothing about why.

This does the lot in one window:

  1. starts the server
  2. polls the port until it actually answers - not "probably up by now"
  3. opens the browser itself, rather than trusting the server to
  4. if it never comes up, prints what the server said on its way down

Run:  python scripts/launch.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("CARTEL_PORT", "8501"))
HOST = "127.0.0.1"
STARTUP_TIMEOUT = 90          # generous: the first run imports a lot


def answering(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_in_use() -> bool:
    return answering(HOST, PORT, timeout=0.5)


def main() -> int:
    if not (ROOT / "app.py").exists():
        print(f" Can't find app.py next to this script (looked in {ROOT}).")
        return 1

    if port_in_use():
        url = f"http://{HOST}:{PORT}"
        print(f" Cartel is already running at {url}")
        print(" Opening your browser.")
        webbrowser.open(url)
        return 0

    print()
    print(" Starting Cartel. The first start takes longest - please wait.")
    print()

    cmd = [sys.executable, "-m", "streamlit", "run", "app.py",
           "--server.headless=true",          # we open the browser ourselves
           f"--server.address={HOST}",
           f"--server.port={PORT}",
           "--browser.gatherUsageStats=false"]

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    captured: list[str] = []

    def pump():
        """Keep the server's own output flowing to the window."""
        assert proc.stdout is not None
        for raw in proc.stdout:
            captured.append(raw)
            sys.stdout.write(raw)
            sys.stdout.flush()

    threading.Thread(target=pump, daemon=True).start()

    started = time.time()
    while time.time() - started < STARTUP_TIMEOUT:
        if proc.poll() is not None:
            print()
            print("=" * 62)
            print(" THE SERVER STOPPED WHILE STARTING UP.")
            print("=" * 62)
            print()
            tail = "".join(captured).strip().splitlines()[-25:]
            if tail:
                print(" What it said:\n")
                for t in tail:
                    print("   " + t)
            else:
                print(" It produced no output at all, which usually means the")
                print(" libraries did not install properly. Run SETUP.bat again.")
            print()
            print(" Send this window to Claude.")
            return 1

        if answering(HOST, PORT):
            url = f"http://{HOST}:{PORT}"
            print()
            print("=" * 62)
            print(" CARTEL IS RUNNING")
            print("=" * 62)
            print(f"\n   {url}\n")
            print(" Your browser should open now. If it doesn't, type the")
            print(" address above into any browser.")
            print()
            print(" ** Leave this window open while you use the app. **")
            print(" ** Press Ctrl+C here when you're finished.        **")
            print("=" * 62)
            print()
            try:
                webbrowser.open(url)
            except Exception:
                pass                      # the address is on screen either way
            break
        time.sleep(0.5)
    else:
        print()
        print(f" Gave up waiting after {STARTUP_TIMEOUT} seconds.")
        print(" The server started but never answered. Send this window to Claude.")
        proc.terminate()
        return 1

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n Stopping Cartel...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

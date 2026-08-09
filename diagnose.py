"""
Why can't the browser reach Cartel?

Run this while START.bat is open. It checks, in order:

  1. Is anything listening on the port at all?
  2. Can Python itself fetch the page, bypassing the browser entirely?
  3. Is a proxy configured that would swallow a localhost request?

Between them those three answer the question. If Python can fetch the page but
your browser can't, the app is fine and the fault is in the browser's network
settings - which is a completely different fix from the app not running.
"""
from __future__ import annotations

import os
import socket
import sys

PORT = int(os.environ.get("CARTEL_PORT", "8501"))
ADDRESSES = [("127.0.0.1", socket.AF_INET, "IPv4"),
             ("::1", socket.AF_INET6, "IPv6")]


def line(char="-"):
    print(char * 62)


def check_listening() -> dict:
    """Try to open a raw socket. Nothing to do with HTTP or the browser."""
    print(f"\n1. Is anything listening on port {PORT}?\n")
    results = {}
    for host, family, label in ADDRESSES:
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, PORT))
            s.close()
            results[label] = True
            print(f"   {label:5} {host:10} CONNECTED")
        except Exception as exc:
            results[label] = False
            print(f"   {label:5} {host:10} refused  ({type(exc).__name__})")
    return results


def check_http() -> dict:
    """Fetch the page with Python, so the browser is out of the picture."""
    print("\n2. Can Python fetch the page itself?\n")
    import urllib.request
    results = {}
    # an opener with NO proxy, so this tests the server and nothing else
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for host, _, label in ADDRESSES:
        url = f"http://{'[' + host + ']' if ':' in host else host}:{PORT}"
        try:
            with opener.open(url, timeout=5) as r:
                results[label] = r.status
                print(f"   {label:5} {url:24} HTTP {r.status}")
        except Exception as exc:
            results[label] = None
            print(f"   {label:5} {url:24} failed  ({type(exc).__name__}: {exc})")
    return results


def check_proxy() -> bool:
    """A proxy that doesn't exempt localhost will refuse this even when the app is fine."""
    print("\n3. Is a proxy configured?\n")
    found = False

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        val = os.environ.get(var) or os.environ.get(var.lower())
        if val:
            found = True
            print(f"   environment {var} = {val}")

    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                found = True
                try:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                except FileNotFoundError:
                    server = "(not set)"
                try:
                    override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                except FileNotFoundError:
                    override = ""
                print(f"   Windows proxy is ON: {server}")
                print(f"   exceptions: {override or '(none)'}")
                if "<local>" not in override and "localhost" not in override:
                    print("   *** localhost is NOT in the exception list ***")
            else:
                print("   Windows proxy is off.")
        except Exception as exc:
            print(f"   could not read the Windows proxy setting ({exc})")

    if not found:
        print("   No proxy found. Not the problem.")
    return found


def verdict(listening: dict, http: dict, proxy: bool) -> None:
    print()
    line("=")
    any_listen = any(listening.values())
    any_http = any(v for v in http.values())

    if not any_listen:
        print(" NOTHING IS LISTENING.\n")
        print(" The app is not running. Either the black window was closed, or")
        print(" START.bat stopped. Double-click START.bat, wait until it says")
        print(" 'You can now view your Streamlit app', leave that window open,")
        print(" and run this check again.")
    elif any_http:
        working = [k for k, v in http.items() if v]
        addr = "127.0.0.1" if "IPv4" in working else "[::1]"
        print(" THE APP IS RUNNING AND SERVING PAGES CORRECTLY.\n")
        print(f" Python fetched the page over {', '.join(working)}.")
        print(f" So the app is fine and the problem is in the browser.\n")
        print(f" Try, in this order:")
        print(f"   1. http://{addr}:{PORT}")
        print(f"   2. a different browser (Chrome or Firefox instead of Edge)")
        if proxy:
            print(f"   3. turn the Windows proxy off, or add 'localhost;127.0.0.1'")
            print(f"      to its exception list  (Settings > Network > Proxy)")
        else:
            print(f"   3. an InPrivate / Incognito window, to rule out an extension")
        print(f"   4. turn off any VPN - they often break localhost")
    else:
        print(" SOMETHING IS LISTENING, BUT IT WILL NOT SERVE A PAGE.\n")
        print(" That usually means antivirus or a firewall is intercepting the")
        print(" connection. Allow python.exe through Windows Firewall, or")
        print(" temporarily disable the antivirus and try again.")
    line("=")
    print(f"\n Port checked: {PORT}")
    print(" Send this whole window to Claude if you're not sure what it means.\n")


if __name__ == "__main__":
    line("=")
    print(" CARTEL - connection check")
    line("=")
    listening = check_listening()
    http = check_http() if any(listening.values()) else {}
    proxy = check_proxy()
    verdict(listening, http, proxy)

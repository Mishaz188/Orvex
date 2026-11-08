# -*- coding: utf-8 -*-
"""
Вбудований тунель для Orvex: доступ з інтернету БЕЗ відкриття портів у роутері.

Режим 1 (за замовчуванням, без акаунтів): «швидкий тунель» Cloudflare.
    Адреса https://xxxx.trycloudflare.com нова при кожному запуску Orvex.
Режим 2 (постійна адреса): іменований тунель Cloudflare. Потрібні безкоштовний акаунт Cloudflare
    і свій домен. У orvex_data/config.json вкажи "cloudflare_token" і "public_url".

Модуль cloudflared (один офіційний файл) Orvex завантажує сам у orvex_data/bin.
"""
import atexit
import json
import os
import platform
import sys
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

import requests

DATA = (Path(sys.executable).parent.resolve() if getattr(sys, "frozen", False) else Path(__file__).parent.resolve()) / "orvex_data"
BIN_DIR = DATA / "bin"
CONFIG_FILE = DATA / "config.json"
CF_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CF_REG = re.compile(r"Registered tunnel connection", re.I)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _asset():
    """(посилання, ім'я файлу) офіційного cloudflared для цієї системи."""
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    if platform.system() == "Windows":
        return base + "cloudflared-windows-amd64.exe", "cloudflared.exe"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    return base + f"cloudflared-linux-{arch}", "cloudflared"


def ensure_cloudflared():
    url, name = _asset()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    exe = BIN_DIR / name
    if exe.exists():
        return exe
    print("⬇ Завантажую модуль тунелю (один раз, ~40 МБ)...")
    tmp = BIN_DIR / (name + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(exe)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


_PROCS = []


def _kill_all():
    for p in _PROCS:
        try:
            p.kill()
        except Exception:
            pass


atexit.register(_kill_all)


def _spawn(cmd, env=None):
    kw = {}
    if platform.system() == "Windows":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1, env=env, **kw)
    _PROCS.append(p)
    return p


def _run(cmd, on_line, env=None):
    p = _spawn(cmd, env)
    try:
        for line in p.stdout:
            on_line(line)
    finally:
        try:
            p.kill()
        except Exception:
            pass


def start_tunnel(port, on_url):
    """on_url(base_url, stable: bool). Тунель перезапускається сам, якщо обірвався."""

    def loop():
        while True:
            cfg = load_config()
            token, public = cfg.get("cloudflare_token", "").strip(), cfg.get("public_url", "").strip().rstrip("/")
            try:
                exe = ensure_cloudflared()
                if token and public:
                    done = {"v": False}

                    def line(l):
                        if not done["v"] and CF_REG.search(l):
                            done["v"] = True
                            on_url(public, True)
                    _run([str(exe), "tunnel", "--no-autoupdate", "run"], line,
                         env={**os.environ, "TUNNEL_TOKEN": token})
                else:
                    def line(l):
                        m = CF_URL.search(l)
                        if m:
                            on_url(m.group(0), False)
                    _run([str(exe), "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"], line)
            except Exception as e:
                print(f"Тунель: {e}")
            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()

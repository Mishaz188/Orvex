# -*- coding: utf-8 -*-
"""
Запуск Orvex подвійним кліком (без bat і без чорного вікна).
Перший раз сам доставить потрібні бібліотеки Python (лише для твого користувача, `pip install --user`),
далі просто відкриває вікно зі сферою.
"""
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).parent.resolve()
os.chdir(HERE)
sys.path.insert(0, str(HERE))

NEEDED = {"faster_whisper": "faster-whisper", "numpy": "numpy", "sounddevice": "sounddevice", "pyttsx3": "pyttsx3", "requests": "requests",
          "pyautogui": "pyautogui", "psutil": "psutil", "pyperclip": "pyperclip", "flask": "flask",
          "mss": "mss", "PIL": "pillow", "qrcode": "qrcode", "edge_tts": "edge-tts"}


def _python():
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")          # під pythonw потрібен python.exe для pip
    return str(cand if cand.exists() else exe)


def _install(packages):
    """Повертає (ok, текст_помилки)."""
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    r = subprocess.run([_python(), "-m", "pip", "install", "--user", "--disable-pip-version-check", *packages],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def _bootstrap():
    missing = [pip for mod, pip in NEEDED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        ok, log = _install(missing)
        if not ok:
            (HERE / "install_error.txt").write_text(log, encoding="utf-8")
            sys.exit(1)
        return

    root = tk.Tk()
    root.title("Orvex")
    root.configure(bg="#02060c")
    root.geometry("420x120")
    tk.Label(root, text="Перший запуск: доставляю бібліотеки…\nЦе одноразово, 1–3 хвилини.",
             bg="#02060c", fg="#9fd8ff", font=("Segoe UI", 12)).pack(expand=True)
    result = {}

    def work():
        result["ok"], result["log"] = _install(missing)
        root.after(0, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()
    if not result.get("ok"):
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Orvex", "Не вдалось встановити бібліотеки:\n\n" + result.get("log", "")[-900:])
        sys.exit(1)


_bootstrap()
import orvex  # noqa: E402

orvex.main()

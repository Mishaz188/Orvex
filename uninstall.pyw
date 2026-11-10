# -*- coding: utf-8 -*-
"""
Деінсталятор Orvex: показує, що Orvex завантажив на цей комп'ютер, і прибирає вибране.
Файли самої програми (orvex.py тощо) він НЕ чіпає. Запуск: двічі клацни uninstall.pyw.
Закрий Orvex перед видаленням.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
DATA = HERE / "orvex_data"

# бібліотеки, які ставить orvex.pyw (requests, pillow, numpy лишаємо за замовчуванням: вони потрібні багатьом програмам)
LIBS = ["faster-whisper", "ctranslate2", "onnxruntime", "sounddevice", "pyttsx3", "comtypes", "edge-tts",
        "pyautogui", "psutil", "pyperclip", "flask", "mss", "qrcode", "vosk"]
SHARED_LIBS = ["requests", "pillow", "numpy"]


def _size(path):
    total = 0
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _hf_models():
    """Папки моделей Whisper у кеші Hugging Face."""
    base = os.environ.get("HUGGINGFACE_HUB_CACHE") or (
        Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub")
    base = Path(base)
    return sorted(base.glob("models--Systran--faster-whisper-*")) if base.exists() else []


def build_plan():
    """[(id, назва, [шляхи], чи_вибрано_за_замовчуванням)]."""
    d = DATA
    return [
        ("whisper", "Модель розпізнавання мови Whisper (кеш Hugging Face)", _hf_models(), True),
        ("old_vosk", "Стара модель Vosk, якщо була", sorted(d.glob("vosk-model*")) if d.exists() else [], True),
        ("tunnel", "Модуль інтернет-тунелю cloudflared", [d / "bin"], True),
        ("logs", "Журнали", [d / "logs"], True),
        ("cache", "Тимчасові файли й QR", [d / "pair_qr.png", d / "model.zip"], True),
        ("data", "Мої дані: налаштування й ключі ШІ (config.json), програми, нотатки, вивчені відповіді, "
                 "підключені телефони", [d / n for n in ("config.json", "programs.txt", "apps.json", "learned.json",
                  "knowledge.json", "notes.txt", "devices.json", "remote_token.txt", "remote_url.txt")], False),
        ("libs", "Бібліотеки Python, які ставив Orvex (" + ", ".join(LIBS) + ")", [], False),
        ("shared", "Також спільні бібліотеки (" + ", ".join(SHARED_LIBS) + ") — лише якщо вони не потрібні іншим програмам", [], False),
    ]


def remove_paths(paths):
    freed = 0
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        freed += _size(p)
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass
    return freed


def pip_uninstall(names):
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.with_name("python.exe").exists() else exe
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    r = subprocess.run([str(py), "-m", "pip", "uninstall", "-y", *names], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    return r.returncode == 0


def run(selected):
    """selected = множина id з build_plan(). Повертає (звільнено_байт, повідомлення)."""
    freed, notes = 0, []
    for pid, _title, paths, _on in build_plan():
        if pid in selected and paths:
            freed += remove_paths(paths)
    if "libs" in selected:
        notes.append("Бібліотеки видалено." if pip_uninstall(LIBS) else "Бібліотеки видалити не вдалось.")
    if "shared" in selected:
        notes.append("Спільні бібліотеки видалено." if pip_uninstall(SHARED_LIBS) else "Спільні бібліотеки не видалились.")
    if DATA.exists() and not any(DATA.iterdir()):
        DATA.rmdir()
    return freed, notes


def _fmt(n):
    return f"{n / 1048576:.0f} МБ" if n >= 1048576 else f"{n / 1024:.0f} КБ"


def main():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Видалення Orvex")
    root.configure(bg="#02060c")
    tk.Label(root, text="Що видалити з цього комп'ютера?\nПерш ніж почати, закрий Orvex.", bg="#02060c", fg="#9fd8ff",
             font=("Segoe UI", 12), justify="left").pack(anchor="w", padx=16, pady=(14, 8))
    vars_ = {}
    for pid, title, paths, on in build_plan():
        size = sum(_size(p) for p in paths)
        v = tk.BooleanVar(value=on)
        vars_[pid] = v
        label = title + (f"  [{_fmt(size)}]" if size else "")
        tk.Checkbutton(root, text=label, variable=v, bg="#02060c", fg="#e8eaf0", selectcolor="#10202e",
                       activebackground="#02060c", activeforeground="#ffffff", wraplength=560, justify="left",
                       anchor="w").pack(anchor="w", padx=16, pady=2)
    tk.Label(root, text="Файли програми (orvex.py та інші) залишаються. Видалити їх можна просто разом з папкою.",
             bg="#02060c", fg="#5d86a8", font=("Segoe UI", 9), wraplength=560, justify="left").pack(anchor="w", padx=16, pady=8)

    def go():
        chosen = {k for k, v in vars_.items() if v.get()}
        if not chosen:
            return
        extra = "\n\nУВАГА: буде видалено ключі ШІ й налаштування." if "data" in chosen else ""
        if not messagebox.askyesno("Orvex", "Видалити вибране?" + extra):
            return
        freed, notes = run(chosen)
        messagebox.showinfo("Orvex", f"Готово. Звільнено приблизно {_fmt(freed)}.\n" + "\n".join(notes))
        root.destroy()

    row = tk.Frame(root, bg="#02060c")
    row.pack(pady=12)
    tk.Button(row, text="Видалити вибране", command=go, width=20).pack(side="left", padx=6)
    tk.Button(row, text="Скасувати", command=root.destroy, width=14).pack(side="left", padx=6)
    root.mainloop()


if __name__ == "__main__":
    main()

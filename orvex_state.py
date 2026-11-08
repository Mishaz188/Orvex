# -*- coding: utf-8 -*-
"""Спільний стан між помічником (фоновий потік) і вікном-сферою (головний потік)."""
STATE = {
    "mode": "idle",     # idle | listening | thinking | speaking | error
    "level": 0.0,       # гучність мікрофона 0..1
    "gui": False,       # вікно запущено
    "qr": None,         # {"matrix": [[bool]], "until": ts, "code": str}  — QR для підключення телефону
    "done": False,      # помічник завершився нормально -> закрити вікно
    "error": "",
}


# --------------------------------------------------------------------------- #
#  Журнали: logs/log.txt (поточні) і logs/latest_log.txt (минулий день)
# --------------------------------------------------------------------------- #
import re as _re
import sys as _sys
import threading as _threading
from datetime import datetime as _dt, timedelta as _td
from pathlib import Path as _Path

_DATE = _re.compile(r"^(\d{4}-\d{2}-\d{2}) ")


class DailyLog:
    """
    log.txt         — усе, що Orvex писав учора й сьогодні (щоб запуск через північ не рвався);
    latest_log.txt  — тільки записи минулого дня (до 23:59:59). Щодоби перезаписується.
    Кожен рядок має дату, тому розділення точне, навіть якщо програму не закривали.
    Розмір обмежений двома добами, чистити вручну не треба.
    """

    def __init__(self, folder, now=None):
        self.dir = _Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "log.txt"
        self.latest = self.dir / "latest_log.txt"
        self._now = now or _dt.now
        self._lock = _threading.RLock()
        self._buf = ""
        self._day = None
        with self._lock:
            self._rotate()

    def _today(self):
        return self._now().strftime("%Y-%m-%d")

    def _rotate(self):
        today = self._today()
        self._day = today
        try:
            lines = self.log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except Exception:
            return
        by_day, cur = {}, None
        for ln in lines:
            m = _DATE.match(ln)
            if m:
                cur = m.group(1)
            if cur:
                by_day.setdefault(cur, []).append(ln)
        past = [d for d in by_day if d < today]
        if past:
            prev = max(past)                       # минулий день (або останній, коли програма не працювала)
            self.latest.write_text("".join(by_day[prev]), encoding="utf-8")
        yesterday = (self._now() - _td(days=1)).strftime("%Y-%m-%d")
        keep = [ln for d in sorted(by_day) if d >= yesterday for ln in by_day[d]]
        if len(keep) != len(lines):
            tmp = self.log.with_suffix(".tmp")
            tmp.write_text("".join(keep), encoding="utf-8")
            tmp.replace(self.log)

    def line(self, text):
        with self._lock:
            if self._today() != self._day:
                self._rotate()
            stamp = self._now().strftime("%Y-%m-%d %H:%M:%S ")
            try:
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(stamp + text.rstrip("\n") + "\n")
            except Exception:
                pass

    def write(self, s):
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                ln, self._buf = self._buf.split("\n", 1)
                if ln.strip():
                    self.line(ln)

    def flush(self):
        with self._lock:
            if self._buf.strip():
                self.line(self._buf)
            self._buf = ""


class _Tee:
    def __init__(self, orig, dl):
        self._o, self._dl = orig, dl

    def write(self, s):
        if self._o is not None:
            try:
                self._o.write(s)
            except Exception:
                pass
        self._dl.write(s)
        return len(s)

    def flush(self):
        if self._o is not None:
            try:
                self._o.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._o, name)


DAILY = None


def init_logs(folder):
    """Запускається один раз: stdout/stderr дублюються в logs/log.txt."""
    global DAILY
    if DAILY is None:
        DAILY = DailyLog(folder)
        _sys.stdout = _Tee(_sys.stdout, DAILY)
        _sys.stderr = _Tee(_sys.stderr, DAILY)
    return DAILY


def log_line(text):
    """Записати рядок лише в журнал (не в консоль)."""
    if DAILY is not None:
        DAILY.line(text)

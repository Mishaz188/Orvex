# -*- coding: utf-8 -*-
"""
Головне вікно Orvex: жива сфера в стилі Джарвіса.
Стани: спокій (синя), слухає (яскрава, реагує на голос), думає (бурштинова, швидко крутиться),
говорить (пульсує), помилка (червона). Подвійний клік / F11 — повний екран, Esc — вийти з нього.
"""
import math
import random
import sys
import time
import tkinter as tk
from pathlib import Path

from orvex_state import STATE

BG = "#02060c"
MODES = {
    "idle":      dict(col=(50, 165, 255),  spin=0.35, amp=0.018, glow=0.55),
    "listening": dict(col=(110, 235, 255), spin=0.75, amp=0.045, glow=0.95),
    "thinking":  dict(col=(255, 170, 55),  spin=2.40, amp=0.030, glow=0.85),
    "speaking":  dict(col=(80, 205, 255),  spin=0.55, amp=0.060, glow=1.00),
    "error":     dict(col=(255, 60, 60),   spin=0.15, amp=0.010, glow=0.50),
}
SEG, CH = 48, 6              # точок у кільці, сегментів у шматку
CORE_N = 9


def _hex(col, k=1.0):
    r, g, b = (max(0, min(255, int(v * k))) for v in col)
    return f"#{r:02x}{g:02x}{b:02x}"


class OrbWindow:
    def __init__(self, holder):
        self.holder = holder
        self.root = tk.Tk()
        self.root.title("Orvex")
        self.root.configure(bg=BG)
        self.root.geometry("640x640")
        self.root.minsize(240, 240)
        try:
            base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
            self.root.iconbitmap(str(base / "orvex.ico"))
        except Exception:
            pass
        self.cv = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.w, self.h = 640, 640
        self.cv.bind("<Configure>", self._resize)
        self.cv.bind("<Double-Button-1>", self._fs)
        self.cv.bind("<Button-1>", self._click)
        self.root.bind("<F11>", self._fs)
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.t, self.last = 0.0, time.time()
        self.rot_y, self.rot_x, self.level = 0.0, 0.4, 0.0
        self.cur = {k: (list(v) if isinstance(v, tuple) else v) for k, v in MODES["idle"].items()}
        self.qr_key, self.closing, self.alive = None, False, True

        # --- точки сфери (спіраль Фібоначчі) ---
        n = 170
        ga = math.pi * (3 - math.sqrt(5))
        self.pts, self.phase = [], []
        for i in range(n):
            y = 1 - 2 * (i + 0.5) / n
            r = math.sqrt(1 - y * y)
            self.pts.append((math.cos(ga * i) * r, y, math.sin(ga * i) * r))
            self.phase.append(random.random() * 6.283)

        c = self.cv
        # порядок створення = порядок малювання (знизу вгору)
        self.ripples = [c.create_oval(0, 0, 1, 1, outline="", width=1) for _ in range(2)]
        self.halo = [c.create_oval(0, 0, 1, 1, outline="", width=1) for _ in range(2)]
        self.arcs = [
            c.create_arc(0, 0, 1, 1, start=0, extent=80, style=tk.ARC, width=3, outline=""),
            c.create_arc(0, 0, 1, 1, start=0, extent=140, style=tk.ARC, width=2, outline=""),
            c.create_arc(0, 0, 1, 1, start=0, extent=34, style=tk.ARC, width=4, outline=""),
            c.create_arc(0, 0, 1, 1, start=0, extent=359.9, style=tk.ARC, width=1, outline="", dash=(2, 9)),
        ]
        self.core = [c.create_oval(0, 0, 1, 1, fill="", outline="") for _ in range(CORE_N)]

        # --- кільця (меридіани й паралелі) ---
        self.rings = []
        for m in range(8):                                   # меридіани
            lam = math.pi * m / 8
            pts = [(math.sin(p) * math.cos(lam), math.cos(p), math.sin(p) * math.sin(lam))
                   for p in (2 * math.pi * s / SEG for s in range(SEG))]
            self._ring(pts)
        for lat in (-0.72, -0.42, -0.14, 0.14, 0.42, 0.72):  # паралелі
            cl, sl = math.cos(math.asin(lat)), lat
            pts = [(cl * math.cos(u), sl, cl * math.sin(u))
                   for u in (2 * math.pi * s / SEG for s in range(SEG))]
            self._ring(pts)
        self.dots = [c.create_oval(0, 0, 1, 1, fill="#000", outline="") for _ in range(n)]

        STATE["gui"] = True
        self.root.after(30, self._tick)

    def _ring(self, pts):
        items = [self.cv.create_line(0, 0, 1, 1, fill="#000", width=1) for _ in range(SEG // CH)]
        self.rings.append((pts, items))

    # ---------------- події ----------------
    def _resize(self, e):
        self.w, self.h = max(100, e.width), max(100, e.height)
        self.qr_key = None

    def _fs(self, _e=None):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def _click(self, _e=None):
        if STATE.get("qr"):
            STATE["qr"] = None

    def _close(self):
        self.closing = True
        o = self.holder.get("o")
        if o is not None:
            o.running = False
        self.alive = False
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()

    # ---------------- анімація ----------------
    def _tick(self):
        if not self.alive:
            return
        t0 = time.time()
        try:
            self._frame()
        except Exception as e:
            print("GUI:", e)
        if (STATE.get("done") or (self.holder.get("o") is not None and not self.holder["o"].running)) \
                and not self.closing:
            self.closing = True
            self.root.after(1200, self._close)
        self.root.after(max(1, int(33 - (time.time() - t0) * 1000)), self._tick)

    def _frame(self):
        now = time.time()
        dt = min(0.1, now - self.last)
        self.last = now
        self.t += dt
        t = self.t
        mode = STATE.get("mode", "idle")
        tgt = MODES.get(mode, MODES["idle"])
        k = min(1.0, dt * 4)
        for i in range(3):
            self.cur["col"][i] += (tgt["col"][i] - self.cur["col"][i]) * k
        for key in ("spin", "amp", "glow"):
            self.cur[key] += (tgt[key] - self.cur[key]) * k
        col, glow, amp = self.cur["col"], self.cur["glow"], self.cur["amp"]

        lvl = STATE.get("level", 0.0) if mode in ("idle", "listening") else 0.0
        self.level += (lvl - self.level) * min(1.0, dt * 10)
        env = 0.0
        if mode == "speaking":
            env = abs(math.sin(t * 6.5) * math.sin(t * 2.3 + 1.0)) * 0.8 + 0.2 * abs(math.sin(t * 11))
        elif mode == "thinking":
            env = 0.35 + 0.15 * math.sin(t * 8)
        energy = self.level + env

        scale = 1 + 0.02 * math.sin(t * 1.6) + 0.10 * energy
        self.rot_y += self.cur["spin"] * dt * (1 + energy)
        self.rot_x = 0.38 + 0.12 * math.sin(t * 0.3)
        cy_, sy_ = math.cos(self.rot_y), math.sin(self.rot_y)
        cx_, sx_ = math.cos(self.rot_x), math.sin(self.rot_x)
        w, h = self.w, self.h
        ox, oy = w / 2, h / 2
        R = min(w, h) * 0.28 * scale
        sf = max(0.6, min(w, h) / 640)
        c = self.cv

        def proj(x, y, z, r=1.0):
            x, y, z = x * r, y * r, z * r
            x1, z1 = x * cy_ + z * sy_, -x * sy_ + z * cy_
            y2, z2 = y * cx_ - z1 * sx_, y * sx_ + z1 * cx_
            f = 3.2 / (3.2 - z2)
            return ox + x1 * f * R, oy + y2 * f * R, z2

        # ядро (світіння)
        for i, item in enumerate(self.core):
            rr = R * (0.92 - i * 0.1)
            b = glow * 0.9 * ((i + 1) / CORE_N) ** 2.2 * (1 + 0.7 * energy)
            c.coords(item, ox - rr, oy - rr, ox + rr, oy + rr)
            c.itemconfigure(item, fill=_hex(col, b))

        # ореол і хвилі
        for j, item in enumerate(self.halo):
            rr = R * (1.12 + 0.11 * j) * (1 + 0.01 * math.sin(t * 2 + j))
            c.coords(item, ox - rr, oy - rr, ox + rr, oy + rr)
            c.itemconfigure(item, outline=_hex(col, 0.18 * glow + 0.25 * energy))
        act = 0.3 if mode != "idle" else 0.08
        for j, item in enumerate(self.ripples):
            ph = (t * 0.5 + j * 0.5) % 1.0
            rr = R * (1.05 + 0.65 * ph)
            br = (1 - ph) ** 1.6 * (act + 0.7 * min(1.0, energy))
            c.coords(item, ox - rr, oy - rr, ox + rr, oy + rr)
            c.itemconfigure(item, outline=_hex(col, br))

        # HUD-дуги
        sp = 0.6 + self.cur["spin"]
        for j, (item, rad, spd, b) in enumerate((
                (self.arcs[0], 1.34, 38, 0.9), (self.arcs[1], 1.47, -26, 0.55),
                (self.arcs[2], 1.60, 71, 1.0), (self.arcs[3], 1.72, 9, 0.4))):
            rr = R * rad
            c.coords(item, ox - rr, oy - rr, ox + rr, oy + rr)
            c.itemconfigure(item, start=(t * spd * sp + j * 57) % 360, outline=_hex(col, b * (0.5 + 0.5 * glow)))

        # кільця
        for pts, items in self.rings:
            pr = [proj(*p) for p in pts]
            for j, item in enumerate(items):
                seg = [pr[(j * CH + s) % SEG] for s in range(CH + 1)]
                zavg = sum(p[2] for p in seg) / len(seg)
                flat = []
                for p in seg:
                    flat += (p[0], p[1])
                c.coords(item, *flat)
                c.itemconfigure(item, fill=_hex(col, (0.10 + 0.50 * (zavg + 1) / 2) * (0.6 + 0.6 * glow)))

        # точки (живі, «дихають»)
        for i, item in enumerate(self.dots):
            x, y, z = self.pts[i]
            ph = self.phase[i]
            disp = 1 + amp * math.sin(t * 2.6 + ph) + energy * 0.12 * math.sin(t * 9 + ph * 3)
            px, py, z2 = proj(x, y, z, disp)
            d = (z2 + 1) / 2
            s = (1.0 + 2.2 * d) * sf
            c.coords(item, px - s, py - s, px + s, py + s)
            c.itemconfigure(item, fill=_hex(col, (0.25 + 0.75 * d) * (0.6 + 0.6 * glow) * (1 + 0.4 * energy)))

        self._overlay(ox, oy)

    # ---------------- QR для підключення телефону + помилка ----------------
    def _overlay(self, ox, oy):
        c = self.cv
        qr = STATE.get("qr")
        if qr and time.time() > qr["until"]:
            STATE["qr"] = qr = None
        key = id(qr["matrix"]) if qr else ("err" if STATE.get("mode") == "error" else None)
        if key == self.qr_key:
            return
        self.qr_key = key
        c.delete("ov")
        if qr:
            m = qr["matrix"]
            size = min(self.w, self.h) * 0.62
            mod = size / len(m)
            x0, y0 = ox - size / 2, oy - size / 2
            pad = mod * 1.5
            c.create_rectangle(x0 - pad, y0 - pad, x0 + size + pad, y0 + size + pad,
                               fill="#ffffff", outline="", tags="ov")
            for r, row in enumerate(m):
                for cc, v in enumerate(row):
                    if v:
                        c.create_rectangle(x0 + cc * mod, y0 + r * mod, x0 + (cc + 1) * mod + 0.5,
                                           y0 + (r + 1) * mod + 0.5, fill="#000000", outline="", tags="ov")
            c.create_text(ox, y0 + size + pad + 22, text="Скануй камерою телефона  ·  клік — закрити",
                          fill="#9fd8ff", font=("Segoe UI", 12), tags="ov")
            c.create_text(ox, y0 + size + pad + 44, text="Код: " + qr.get("code", ""),
                          fill="#5d86a8", font=("Consolas", 11), tags="ov")
        elif key == "err":
            c.create_text(ox, self.h - 28, text=(STATE.get("error") or "Помилка. Дивись orvex_data/logs/log.txt")[:140],
                          fill="#ff7b7b", font=("Segoe UI", 11), tags="ov")

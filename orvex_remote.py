# -*- coding: utf-8 -*-
"""
Orvex Remote: захищений веб-застосунок (PWA) для телефону + ярлик для годинника.

Безпека (чому сторонні не мають доступу):
  * Сервер слухає тільки localhost (LAN відкривається лише з ORVEX_LAN=1) і виходить в інтернет
    через вихідний тунель: у роутері нічого відкривати не треба.
  * Без підключеного пристрою всі функції повертають 401. Пристрій підключається ОДНОРАЗОВИМ
    кодом (QR), який показується тільки на екрані ноутбука і діє 5 хвилин.
    Додати пристрій можна лише голосом на самому ноутбуці: «Орвекс, додай телефон».
  * Пристрій отримує випадковий ключ у HttpOnly+Secure+SameSite=Strict cookie (на сервері лише хеш).
  * 5 хибних спроб коду знищують код; IP-блокування; перевірка Origin; суворі заголовки (CSP).
  * Кнопка/команда «забудь усі пристрої» миттєво відключає все.
  * Журнал дій: orvex_data/logs/log.txt. Терміналу на сторінці немає взагалі.
  * Годинник користується окремим ключем, який дозволяє ТІЛЬКИ текстові команди Orvex.
"""
import hashlib
import io
import json
import logging
import os
import platform
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path

import mss
import pyautogui
from flask import Flask, Response, g, jsonify, request
from PIL import Image, ImageDraw

from orvex_state import STATE

try:
    import psutil
except ImportError:
    psutil = None

try:                                   # Windows: фізичні пікселі = координати курсора = координати кадру
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

OS = platform.system()
PORT = int(os.environ.get("ORVEX_PORT", "8765"))
ALLOW_WATCH = os.environ.get("ORVEX_WATCH", "1") != "0"
USE_TUNNEL = os.environ.get("ORVEX_TUNNEL", "1") != "0"
USE_LAN = os.environ.get("ORVEX_LAN", "0") == "1" or not USE_TUNNEL

DATA = (Path(sys.executable).parent.resolve() if getattr(sys, "frozen", False) else Path(__file__).parent.resolve()) / "orvex_data"
DATA.mkdir(exist_ok=True)
DEV_FILE = DATA / "devices.json"
WATCH_FILE = DATA / "remote_token.txt"       # ключ для годинника
CONFIG_FILE = DATA / "config.json"
COOKIE = "orvex_dev"

if WATCH_FILE.exists():
    WATCH_KEY = WATCH_FILE.read_text().strip()
else:
    WATCH_KEY = secrets.token_urlsafe(32)
    WATCH_FILE.write_text(WATCH_KEY)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

BASE = {"url": None, "stable": False}
_pair = {"hash": None, "exp": 0.0, "tries": 0}
_pair_lock = threading.Lock()
_dev_lock = threading.Lock()
_rl = {}                      # ip -> (fails, until)

# --------------------------------------------------------------------------- #
#  Допоміжне
# --------------------------------------------------------------------------- #
def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _ip():
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


def audit(what):
    try:
        who = getattr(g, "dev", None) or "-"
        from orvex_state import log_line
        log_line(f"[remote] {_ip()} dev={who} {what}")
    except Exception:
        pass


def _load_devices():
    try:
        return json.loads(DEV_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_devices(d):
    DEV_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    try:
        os.chmod(DEV_FILE, 0o600)
    except Exception:
        pass


def device_count():
    return len(_load_devices())


def revoke_all():
    with _dev_lock:
        _save_devices({})
    with _pair_lock:
        _pair.update(hash=None, exp=0.0, tries=0)
    STATE["qr"] = None


def _blocked(ip):
    n, until = _rl.get(ip, (0, 0))
    return n >= 5 and time.time() < until


def _fail(ip):
    n, until = _rl.get(ip, (0, 0))
    if time.time() >= until and n >= 5:
        n = 0
    _rl[ip] = (n + 1, time.time() + 60)


def _current_device():
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    h = _sha(raw)
    if h in _load_devices():
        return h[:8]
    return None


def device_auth(f):
    @wraps(f)
    def wrapper(*a, **k):
        dev = _current_device()
        if not dev:
            return jsonify(ok=False, error="auth"), 401
        g.dev = dev
        return f(*a, **k)
    return wrapper


@app.before_request
def _origin_check():
    if request.path.startswith("/ext/"):          # розширення Chrome: власна перевірка в _ext_local()
        return None
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return jsonify(ok=False, error="origin"), 403


@app.after_request
def _headers(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; manifest-src 'self'; "
        "worker-src 'self'; frame-ancestors 'none'")
    return resp

# --------------------------------------------------------------------------- #
#  Підключення пристроїв (pairing)
# --------------------------------------------------------------------------- #
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_pairing():
    code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    with _pair_lock:
        _pair.update(hash=_sha(code), exp=time.time() + 300, tries=0)
    return code


@app.post("/api/pair")
def api_pair():
    ip = _ip()
    if _blocked(ip):
        return jsonify(ok=False, error="Забагато спроб, зачекай хвилину"), 429
    code = ((request.get_json(silent=True) or {}).get("code") or "").strip().upper().replace("-", "")
    with _pair_lock:
        valid = (_pair["hash"] and time.time() < _pair["exp"] and _pair["tries"] < 5)
        ok = bool(valid and code and secrets.compare_digest(_sha(code), _pair["hash"]))
        if ok:
            _pair.update(hash=None, exp=0.0, tries=0)         # одноразовий
        elif valid:
            _pair["tries"] += 1
            if _pair["tries"] >= 5:
                _pair.update(hash=None, exp=0.0)              # забагато спроб: код знищено
    if not ok:
        _fail(ip)
        g.dev = None
        audit("PAIR FAIL")
        return jsonify(ok=False, error="Код невірний або прострочений. Скажи «Орвекс, додай телефон»."), 403
    raw = secrets.token_urlsafe(32)
    ua = (request.headers.get("User-Agent") or "")[:80]
    with _dev_lock:
        d = _load_devices()
        d[_sha(raw)] = {"ua": ua, "created": time.strftime("%Y-%m-%d %H:%M")}
        _save_devices(d)
    g.dev = _sha(raw)[:8]
    STATE["qr"] = None
    audit("PAIR OK " + ua)
    resp = jsonify(ok=True)
    secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
    resp.set_cookie(COOKIE, raw, max_age=365 * 24 * 3600, httponly=True,
                    secure=secure, samesite="Strict", path="/")
    return resp


@app.get("/api/me")
@device_auth
def api_me():
    return jsonify(ok=True)


@app.post("/api/logout")
@device_auth
def api_logout():
    raw = request.cookies.get(COOKIE, "")
    with _dev_lock:
        d = _load_devices()
        d.pop(_sha(raw), None)
        _save_devices(d)
    audit("LOGOUT")
    resp = jsonify(ok=True)
    resp.delete_cookie(COOKIE, path="/")
    return resp

# --------------------------------------------------------------------------- #
#  Міст до розширення Chrome (вкладки й групи вкладок)
# --------------------------------------------------------------------------- #
_EXT = {"q": queue.Queue(), "wait": {}, "seen": 0.0}


def _ext_local():
    """Лише розширення Chrome на цьому ж ноутбуці: не з інтернету (тунель додає свої заголовки)."""
    host = (request.host or "").lower().rsplit(":", 1)[0]
    if request.remote_addr not in ("127.0.0.1", "::1") or host not in ("127.0.0.1", "localhost"):
        return False
    if any(request.headers.get(h) for h in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "Forwarded")):
        return False
    # власний заголовок: сайт у браузері не може його надіслати без CORS-дозволу, якого сервер не дає
    origin = request.headers.get("Origin")
    return request.headers.get("X-Orvex-Ext") == "1" and (not origin or origin.startswith("chrome-extension://"))


@app.get("/ext/poll")
def ext_poll():
    if not _ext_local():
        return "", 403
    _EXT["seen"] = time.time()
    try:
        cmd = _EXT["q"].get(timeout=20)
    except queue.Empty:
        _EXT["seen"] = time.time()
        return "", 204
    return jsonify(cmd)


@app.post("/ext/result")
def ext_result():
    if not _ext_local():
        return "", 403
    _EXT["seen"] = time.time()
    data = request.get_json(silent=True) or {}
    w = _EXT["wait"].get(data.get("id"))
    if w:
        w["res"] = data
        w["e"].set()
    return jsonify(ok=True)


def chrome_connected():
    return time.time() - _EXT["seen"] < 45


def chrome_call(op, timeout=8, **args):
    """Виконує дію у Chrome через розширення. Повертає дані або кидає помилку з причиною."""
    if not chrome_connected():
        raise RuntimeError("розширення Chrome не підключене (див. інструкцію: папка orvex_chrome)")
    cid = secrets.token_hex(6)
    w = {"e": threading.Event(), "res": None}
    _EXT["wait"][cid] = w
    try:
        _EXT["q"].put({"id": cid, "op": op, "args": args})
        if not w["e"].wait(timeout):
            raise TimeoutError("Chrome не відповів")
    finally:
        _EXT["wait"].pop(cid, None)
    res = w["res"] or {}
    try:
        from orvex_state import log_line
        log_line(f"[chrome] {op} {json.dumps(args, ensure_ascii=False)[:200]} -> {'ok' if res.get('ok') else res.get('error')}")
    except Exception:
        pass
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "помилка Chrome")
    return res.get("data")


# --------------------------------------------------------------------------- #
#  Екран
# --------------------------------------------------------------------------- #
def _monitor(sct, idx):
    idx = min(max(idx, 1), len(sct.monitors) - 1)
    return sct.monitors[idx]


_ARROW = [(0, 0), (0, 16), (4, 12), (7, 19), (9, 18), (6, 11), (11, 11)]


def _draw_cursor(img, pos, src_w):
    """mss не знімає курсор, тому малюємо стрілку самі (позицію бере pyautogui)."""
    try:
        k = img.width / src_w
        cx, cy = pos[0] * k, pos[1] * k
        if not (0 <= cx < img.width and 0 <= cy < img.height):
            return
        s = max(0.9, img.width / 900)           # розмір стрілки ~ пропорційний до кадру
        pts = [(cx + x * s * 1.3, cy + y * s * 1.3) for x, y in _ARROW]
        d = ImageDraw.Draw(img)
        d.polygon(pts, fill="#ffffff", outline="#000000")
    except Exception:
        pass


def _grab(sct, mon, width):
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > width:
        img = img.resize((width, int(img.height * width / img.width)), Image.BILINEAR)
    try:
        p = pyautogui.position()
        _draw_cursor(img, (p[0] - mon["left"], p[1] - mon["top"]), shot.size[0])
    except Exception:
        pass
    return img


def _enc(img, quality):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


@app.get("/api/frame")
@device_auth
def api_frame():
    """Один кадр екрана. ETag/304: якщо екран не змінився, нічого не передаємо."""
    width = min(max(int(request.args.get("w", 1000)), 320), 2560)
    quality = min(max(int(request.args.get("q", 50)), 10), 90)
    with mss.mss() as sct:
        img = _grab(sct, _monitor(sct, int(request.args.get("m", 1))), width)
    etag = '"' + hashlib.md5(img.tobytes() + f"{width}-{quality}".encode()).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    resp = Response(_enc(img, quality), mimetype="image/jpeg")
    resp.headers["ETag"] = etag
    return resp


def _abs(x, y, midx=1):
    with mss.mss() as sct:
        m = _monitor(sct, midx)
    return m["left"] + int(float(x) * m["width"]), m["top"] + int(float(y) * m["height"])

# --------------------------------------------------------------------------- #
#  Керування
# --------------------------------------------------------------------------- #
@app.post("/api/mouse")
@device_auth
def api_mouse():
    d = request.get_json(silent=True) or {}
    act, midx = d.get("action"), int(d.get("m", 1))
    if act == "scroll":
        pyautogui.scroll(int(d.get("dy", 0)))
        return jsonify(ok=True)
    x, y = _abs(d.get("x", 0.5), d.get("y", 0.5), midx)
    if act == "move":
        pyautogui.moveTo(x, y)
    elif act == "click":
        pyautogui.click(x, y)
    elif act == "rclick":
        pyautogui.rightClick(x, y)
    elif act == "dclick":
        pyautogui.doubleClick(x, y)
    elif act == "drag":
        x2, y2 = _abs(d.get("x2", 0.5), d.get("y2", 0.5), midx)
        pyautogui.moveTo(x, y)
        pyautogui.dragTo(x2, y2, duration=0.3, button="left")
    else:
        return jsonify(ok=False, error="невідома дія"), 400
    audit(f"MOUSE {act}")
    return jsonify(ok=True)


def _unicode_win(text):
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]       # MOUSEINPUT найбільший: правильний розмір INPUT

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32 = ctypes.windll.user32
    UNICODE, KEYUP, VK_RETURN = 0x0004, 0x0002, 0x0D

    def key(vk, scan, flags):
        i = INPUT()
        i.type = 1                                                # INPUT_KEYBOARD
        i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def send(items):
        arr = (INPUT * len(items))(*items)
        if user32.SendInput(len(items), arr, ctypes.sizeof(INPUT)) != len(items):
            raise OSError("SendInput не спрацював")

    data = text.encode("utf-16-le")
    for k in range(0, len(data), 2):
        cu = data[k] | (data[k + 1] << 8)
        if cu == 0x0D:                                            # \r пропускаємо
            continue
        if cu == 0x0A:                                            # новий рядок = Enter
            send([key(VK_RETURN, 0, 0), key(VK_RETURN, 0, KEYUP)])
        else:
            send([key(0, cu, UNICODE), key(0, cu, UNICODE | KEYUP)])
        time.sleep(0.003)


def _paste(text):
    """Друк тексту (кирилиця теж). Windows: прямий Unicode-ввід; інакше буфер обміну; запас: ASCII."""
    if not text:
        return
    if OS == "Windows":
        try:
            _unicode_win(text)
            return
        except Exception as e:
            print("Unicode-ввід не вдався:", e)
    try:
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.08)                      # дати системі прийняти буфер
        pyautogui.hotkey("command" if OS == "Darwin" else "ctrl", "v")
        return
    except Exception as e:
        print("Буфер обміну не спрацював:", e)
    pyautogui.write(text, interval=0.01)      # лише латиниця


@app.post("/api/type")
@device_auth
def api_type():
    d = request.get_json(silent=True) or {}
    text = d.get("text", "")
    if text:
        _paste(text)
    if d.get("enter"):
        pyautogui.press("enter")
    audit(f"TYPE {len(text)} символів")
    return jsonify(ok=True)


@app.post("/api/hotkey")
@device_auth
def api_hotkey():
    keys = (request.get_json(silent=True) or {}).get("keys") or []
    if not keys:
        return jsonify(ok=False), 400
    if OS == "Darwin":
        keys = ["command" if k == "ctrl" else "option" if k == "alt" else k for k in keys]
    pyautogui.hotkey(*keys)
    audit("HOTKEY " + "+".join(keys))
    return jsonify(ok=True)


@app.post("/api/command")
@device_auth
def api_command():
    text = (request.get_json(silent=True) or {}).get("text", "")
    audit("CMD " + text)
    return jsonify(app.config["ORVEX"].run_remote(text))


@app.get("/cmd")
def cmd_watch():
    """Для годинника: ключ у заголовку X-Key (краще) або ?k=. Дозволяє ЛИШЕ текстові команди."""
    if not ALLOW_WATCH:
        return "Вимкнено", 403
    ip = _ip()
    if _blocked(ip):
        return "Забагато спроб", 429
    key = request.headers.get("X-Key") or request.args.get("k") or ""
    if not secrets.compare_digest(key.encode(), WATCH_KEY.encode()):
        _fail(ip)
        return "Невірний ключ", 401
    g.dev = "watch"
    text = request.args.get("text", "")
    audit("WATCH " + text)
    res = app.config["ORVEX"].run_remote(text)
    if request.args.get("plain"):
        return Response(res["reply"], mimetype="text/plain; charset=utf-8")
    return jsonify(res)


@app.get("/api/status")
@device_auth
def api_status():
    s = {"host": socket.gethostname(), "time": time.strftime("%H:%M")}
    if psutil:
        s["cpu"] = psutil.cpu_percent(interval=0.2)
        s["ram"] = psutil.virtual_memory().percent
        b = psutil.sensors_battery()
        if b:
            s["battery"] = int(b.percent)
            s["plugged"] = bool(b.power_plugged)
    return jsonify(s)

# --------------------------------------------------------------------------- #
#  PWA: сторінка, маніфест, іконки
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify(
        name="Orvex", short_name="Orvex", start_url="/", scope="/", display="standalone",
        background_color="#0f1115", theme_color="#0f1115",
        icons=[{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
               {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}])


@app.get("/sw.js")
def sw():
    js = ("self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));"
          "self.addEventListener('fetch',e=>{});")
    return Response(js, mimetype="text/javascript")


_icons = {}


def _icon(size):
    if size not in _icons:
        img = Image.new("RGB", (size, size), "#0f1115")
        d = ImageDraw.Draw(img)
        m = size * 0.2
        d.ellipse([m, m, size - m, size - m], outline="#6ea8ff", width=max(2, int(size * 0.07)))
        c, r = size / 2, size * 0.085
        d.ellipse([c - r, c - r, c + r, c + r], fill="#6ea8ff")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _icons[size] = buf.getvalue()
    return _icons[size]


for _s, _p in ((192, "/icon-192.png"), (512, "/icon-512.png"), (180, "/apple-touch-icon.png")):
    app.add_url_rule(_p, endpoint="icon%d" % _s,
                     view_func=(lambda s=_s: Response(_icon(s), mimetype="image/png",
                                                      headers={"Cache-Control": "public, max-age=86400"})))


PAGE = r"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="theme-color" content="#0f1115">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Orvex">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/icon-192.png">
<title>Orvex</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--fg:#e8eaf0;--ac:#6ea8ff;--mut:#8a90a0;--line:#2b303b;--me:#2a4a86;--bot:#1a1d24}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--card:#fff;--fg:#14171c;--ac:#2f6bff;--mut:#667085;--line:#dfe3ea;--me:#dbe7ff;--bot:#fff}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:16px system-ui,-apple-system,sans-serif;display:flex;flex-direction:column;height:100dvh;overflow:hidden}
[hidden]{display:none!important}
header{display:flex;align-items:center;gap:10px;padding:calc(env(safe-area-inset-top) + 8px) 14px 8px;border-bottom:1px solid var(--line)}
header b{font-size:18px}.dot{width:9px;height:9px;border-radius:50%;background:#3ccf7a;display:inline-block;margin-right:6px}
#st{margin-left:auto;color:var(--mut);font-size:12px;text-align:right}
button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 13px;white-space:nowrap}
button.on{background:var(--ac);border-color:var(--ac);color:#fff}
input,select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 13px;min-width:0}
main{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column}
section{flex:1;min-height:0;display:flex;flex-direction:column;overflow:auto}
#msgs{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.m{max-width:86%;padding:10px 14px;border-radius:18px;line-height:1.4;white-space:pre-wrap;word-break:break-word}
.m.me{align-self:flex-end;background:var(--me);border-bottom-right-radius:6px}
.m.bot{align-self:flex-start;background:var(--bot);border:1px solid var(--line);border-bottom-left-radius:6px}
.hint{color:var(--mut);text-align:center;padding:30px 20px;font-size:14px}
.chips{display:flex;gap:8px;padding:6px 12px;overflow-x:auto}.chips button{border-radius:20px;font-size:14px}
#composer{display:flex;gap:8px;padding:8px 12px 10px;border-top:1px solid var(--line)}
#composer input{flex:1;border-radius:22px}#composer button{border-radius:50%;width:46px;height:46px;padding:0;font-size:19px}
#mic.on{background:#e5484d;border-color:#e5484d;animation:p 1s infinite}@keyframes p{50%{opacity:.6}}
nav{display:flex;border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom);background:var(--card)}
nav button{flex:1;border:0;border-radius:0;background:none;padding:12px;color:var(--mut)}nav button.on{color:var(--ac);background:none}
#wrap{background:#000;touch-action:none;flex:none;min-height:120px}
#screen{width:100%;display:block;user-select:none;-webkit-user-drag:none;-webkit-touch-callout:none}
.row{display:flex;gap:6px;padding:7px 10px;overflow-x:auto;flex:none}.row input{flex:1}
#pair{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;padding:24px;background:var(--bg);z-index:9}
.card{max-width:380px;width:100%;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px}
.card h2{margin:0 0 8px}.card p{color:var(--mut);font-size:14px;line-height:1.45}.card input{width:100%;margin:10px 0;text-transform:uppercase;letter-spacing:2px;text-align:center}
.card button{width:100%;background:var(--ac);border-color:var(--ac);color:#fff}
#err{color:#e5484d;font-size:14px;min-height:20px}
#sheet{position:fixed;right:10px;top:calc(env(safe-area-inset-top) + 48px);background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px;z-index:8;display:flex;flex-direction:column;gap:4px;box-shadow:0 8px 30px #0006}
#sheet button{border:0;text-align:left;background:none}
#stage{display:flex;flex-direction:column;flex:none;position:relative}
.onlyfull,#handle{display:none}
body.full header,body.full nav,body.full #composer{display:none!important}
body.full main{flex:1;min-height:0}
body.full #tab-screen{overflow:hidden}
body.full #stage{flex:1;min-height:0;flex-direction:row;background:#000;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
body.full #wrap{flex:1;min-width:0;min-height:0;height:100%;display:flex}
body.full #screen{width:100%;height:100%;object-fit:contain}
body.full #panel{flex:none;width:0;overflow:hidden;transition:width .25s ease;background:var(--bg);border-left:1px solid var(--line)}
body.full.drawer #panel{width:min(270px,58vw)}
body.full #panelin{width:min(270px,58vw);height:100%;overflow-y:auto;padding:8px 4px}
body.full #panel .row{flex-wrap:wrap;overflow:visible}
body.full #panel .row input{min-width:120px}
body.full .onlyfull{display:block}
body.full #handle{display:block;position:absolute;top:50%;right:0;transform:translateY(-50%);z-index:5;width:26px;height:64px;padding:0;border-radius:12px 0 0 12px;opacity:.8;transition:right .25s ease}
body.full.drawer #handle{right:min(270px,58vw)}
</style></head><body>

<div id="pair" hidden><div class="card">
  <h2>🤖 Orvex</h2>
  <p>Цей пристрій ще не підключено. На ноутбуці скажи <b>«Орвекс, додай телефон»</b>. З'явиться QR-код: відскануй його камерою телефону або введи код нижче.</p>
  <input id="code" placeholder="КОД" autocomplete="off" autocapitalize="characters" maxlength="12">
  <div id="err"></div>
  <button id="pairbtn">Підключити</button>
</div></div>

<div id="app" hidden style="display:contents">
<header><b><span class="dot"></span>Orvex</b><span id="st"></span><button id="menu" style="padding:4px 10px">⋯</button></header>
<div id="sheet" hidden>
  <button id="spk">🔊 Озвучувати відповіді: <span id="spkv">так</span></button>
  <button id="logout">🚪 Відключити цей пристрій</button>
</div>

<main>
 <section id="tab-chat">
  <div id="msgs"><div class="hint">Напиши або продиктуй команду.<br>Наприклад: «відкрий ютуб», «зроби скріншот», «яка зараз година».</div></div>
  <div class="chips">
    <button data-c="зроби скріншот">📸 Скріншот</button><button data-c="гучніше">🔊 Гучніше</button>
    <button data-c="тихіше">🔉 Тихіше</button><button data-c="пауза">⏯ Пауза</button>
    <button data-c="заблокуй екран">🔒 Блокування</button><button data-c="який заряд">🔋 Заряд</button>
  </div>
 </section>
 <section id="tab-screen" hidden>
  <div id="stage">
  <div id="wrap"><img id="screen" alt=""></div>
  <div id="panel"><div id="panelin">
  <div class="row onlyfull"><button id="fsx" style="width:100%">⤡ Вийти з повного екрана</button></div>
  <div class="row" id="modes">
    <button data-m="click" class="on">Клік</button><button data-m="rclick">ПКМ</button><button data-m="dclick">Подвійний</button>
    <button data-m="scroll">Скрол</button><button data-m="drag">Перетяг.</button>
    <select id="qual"><option value="3|700|40">Економ</option><option value="5|1000|50" selected>Норма</option><option value="8|1600|65">Висока</option></select>
    <button id="fs">⛶</button>
  </div>
  <div class="row"><input id="txt" placeholder="Текст у ноутбук…" enterkeyhint="send"><button id="sendt">⌨</button><button id="erase">⌫ Стерти</button></div>
  <div class="row" id="keys">
    <button data-k="win+tab">🗂 Win+Tab</button><button data-k="ctrl+z">Ctrl+Z</button>
    <button data-k="esc">Esc</button><button data-k="tab">Tab</button><button data-k="enter">Enter</button>
    <button data-k="up">↑</button><button data-k="down">↓</button><button data-k="left">←</button><button data-k="right">→</button>
  </div>
  </div></div>
  <button id="handle" title="Меню">◂</button>
  </div>
 </section>
</main>

<div id="composer"><input id="cmd" placeholder="Команда для Orvex…" enterkeyhint="send" autocomplete="off">
  <button id="mic" title="Голос">🎤</button><button id="send" style="background:var(--ac);border-color:var(--ac);color:#fff">➤</button></div>
<nav><button data-tab="chat" class="on">💬 Чат</button><button data-tab="screen">🖥 Екран</button></nav>
</div>

<script>
const $ = s => document.querySelector(s);
try { navigator.serviceWorker && navigator.serviceWorker.register('/sw.js'); } catch(e){}

async function api(path, body, method){
  const r = await fetch('/api/' + path, {method: method || 'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: method === 'GET' ? undefined : JSON.stringify(body || {})});
  if (r.status === 401){ showPair(); throw new Error('auth'); }
  return r.json().catch(() => ({}));
}

// ---------- підключення ----------
function showPair(msg){ stopStream(); $('#app').hidden = true; $('#pair').hidden = false; $('#err').textContent = msg || ''; }
async function doPair(code){
  const r = await fetch('/api/pair', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code})});
  const j = await r.json().catch(() => ({}));
  if (j.ok){ $('#pair').hidden = true; boot(); } else { $('#err').textContent = j.error || 'Помилка'; }
}
$('#pairbtn').onclick = () => doPair($('#code').value);
$('#code').onkeydown = e => { if (e.key === 'Enter') doPair($('#code').value); };

async function boot(){
  const r = await fetch('/api/me', {credentials:'same-origin'});
  if (r.ok){
    const j = await r.json();
    $('#pair').hidden = true; $('#app').hidden = false; 
    status();
  } else {
    $('#pair').hidden = false;
  }
}
const m = location.hash.match(/pair=([A-Za-z0-9]+)/);
if (m){ history.replaceState(null, '', location.pathname); doPair(m[1]); } else { boot(); }

// ---------- вкладки ----------
let tab = 'chat';
function setTab(t){
  tab = t;
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  $('#tab-chat').hidden = t !== 'chat'; $('#tab-screen').hidden = t !== 'screen'; $('#composer').hidden = t !== 'chat';
  t === 'screen' ? startStream() : stopStream();
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => setTab(b.dataset.tab));
document.addEventListener('visibilitychange', () => { document.hidden ? stopStream() : (tab === 'screen' && startStream()); });

// ---------- чат ----------
let speak = true;
function add(role, text){
  const h = $('#msgs .hint'); if (h) h.remove();
  const d = document.createElement('div'); d.className = 'm ' + role; d.textContent = text;
  $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d;
}
function say(text){
  if (!speak || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text); u.lang = 'uk-UA'; speechSynthesis.cancel(); speechSynthesis.speak(u);
}
async function sendCmd(text){
  text = (text || '').trim(); if (!text) return;
  add('me', text);
  const wait = add('bot', '…');
  try {
    const r = await api('command', {text});
    wait.textContent = r.reply || 'Готово'; say(wait.textContent);
    if (r.running === false) add('bot', 'Orvex на ноутбуці вимкнено.');
  } catch(e){ wait.textContent = 'Немає зв’язку з ноутбуком.'; }
}
$('#send').onclick = () => { sendCmd($('#cmd').value); $('#cmd').value = ''; };
$('#cmd').onkeydown = e => { if (e.key === 'Enter') $('#send').click(); };
document.querySelectorAll('.chips button').forEach(b => b.onclick = () => sendCmd(b.dataset.c));

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR){
  const rec = new SR(); rec.lang = 'uk-UA'; rec.interimResults = false;
  rec.onresult = e => sendCmd(e.results[0][0].transcript);
  rec.onend = () => $('#mic').classList.remove('on');
  rec.onerror = () => $('#mic').classList.remove('on');
  $('#mic').onclick = () => { try { speechSynthesis.cancel(); $('#mic').classList.add('on'); rec.start(); } catch(e){ rec.stop(); } };
} else {
  $('#mic').onclick = () => alert('Цей браузер не вміє розпізнавати мову. Натисни поле команди й скористайся мікрофоном на клавіатурі телефону.');
}

// ---------- меню ----------
$('#menu').onclick = () => { $('#sheet').hidden = !$('#sheet').hidden; };
$('#spk').onclick = () => { speak = !speak; $('#spkv').textContent = speak ? 'так' : 'ні'; };
$('#logout').onclick = async () => { if (confirm('Відключити цей пристрій від Orvex?')){ await api('logout'); showPair(); } };
document.addEventListener('click', e => { if (!e.target.closest('#sheet') && e.target.id !== 'menu') $('#sheet').hidden = true; });

// ---------- екран ----------
const scr = $('#screen');
let loopId = 0, etag = '', lastUrl = null;
function startStream(){
  const id = ++loopId; etag = '';
  const [fps, w, q] = $('#qual').value.split('|');
  (async function loop(){
    let fail = 0;
    while (id === loopId){
      const t0 = performance.now();
      try {
        const r = await fetch(`/api/frame?w=${w}&q=${q}`, {credentials:'same-origin', cache:'no-store',
                              headers: etag ? {'If-None-Match': etag} : {}});
        if (id !== loopId) return;
        if (r.status === 401){ showPair(); return; }
        if (r.status === 200){
          etag = r.headers.get('ETag') || '';
          const u = URL.createObjectURL(await r.blob());
          scr.src = u; if (lastUrl) URL.revokeObjectURL(lastUrl); lastUrl = u;
        }
        fail = 0;
        const gap = 1000 / fps - (performance.now() - t0);
        await new Promise(s => setTimeout(s, r.status === 304 ? Math.max(gap, 250) : Math.max(gap, 0)));
      } catch(e){ fail++; await new Promise(s => setTimeout(s, Math.min(4000, 500 * fail))); }
    }
  })();
}
function stopStream(){ loopId++; }
$('#qual').onchange = startStream;

let mode = 'click', st = null;
document.querySelectorAll('#modes button[data-m]').forEach(b => b.onclick = () => {
  mode = b.dataset.m;
  document.querySelectorAll('#modes button[data-m]').forEach(x => x.classList.toggle('on', x === b));
});
const clamp01 = v => Math.min(1, Math.max(0, v));
function norm(e){      // координати точки на ЗОБРАЖЕННІ (враховує смуги по краях при object-fit: contain)
  const rc = scr.getBoundingClientRect();
  const nw = scr.naturalWidth || rc.width, nh = scr.naturalHeight || rc.height;
  const k = Math.min(rc.width / nw, rc.height / nh), dw = nw * k, dh = nh * k;
  const ox = rc.left + (rc.width - dw) / 2, oy = rc.top + (rc.height - dh) / 2;
  return [clamp01((e.clientX - ox) / dw), clamp01((e.clientY - oy) / dh)];
}
scr.addEventListener('pointerdown', e => { scr.setPointerCapture(e.pointerId); const [px, py] = norm(e); st = {x:px, y:py, cy:e.clientY, t:Date.now()}; e.preventDefault(); });
scr.addEventListener('pointermove', e => {
  if (mode === 'scroll' && st){
    const dy = st.cy - e.clientY;
    if (Math.abs(dy) > 14){ api('mouse', {action:'scroll', dy: -Math.round(dy / 14)}); st.cy = e.clientY; }
  }
});
scr.addEventListener('pointerup', e => {
  if (!st) return;
  const x = st.x, y = st.y, long = Date.now() - st.t > 600;
  if (mode === 'click') api('mouse', {action: long ? 'rclick' : 'click', x, y});
  else if (mode === 'rclick' || mode === 'dclick') api('mouse', {action: mode, x, y});
  else if (mode === 'drag'){ const [x2, y2] = norm(e); api('mouse', {action:'drag', x, y, x2, y2}); }
  st = null;
});
$('#sendt').onclick = () => { api('type', {text: $('#txt').value}); $('#txt').value = ''; };
$('#txt').onkeydown = e => { if (e.key === 'Enter'){ api('type', {text: $('#txt').value, enter:true}); $('#txt').value = ''; } };
document.querySelectorAll('#keys button').forEach(b => b.onclick = () => api('hotkey', {keys: b.dataset.k.split('+')}));
(function(){                                   // «Стерти»: тап = 1 символ, утримання = безперервно
  const b = $('#erase'), send = () => api('hotkey', {keys: ['backspace']});
  let t1 = null, t2 = null;
  const stop = () => { clearTimeout(t1); clearInterval(t2); };
  b.addEventListener('pointerdown', e => { e.preventDefault(); send(); t1 = setTimeout(() => { t2 = setInterval(send, 110); }, 420); });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => b.addEventListener(ev, stop));
})();
let fsApi = false;
function setFull(on){
  document.body.classList.toggle('full', on);
  if (!on){ document.body.classList.remove('drawer'); $('#handle').textContent = '◂'; }
  try {
    if (on && document.documentElement.requestFullscreen){
      document.documentElement.requestFullscreen().then(() => {
        fsApi = true;
        try { screen.orientation.lock('landscape').catch(() => {}); } catch(e){}
      }).catch(() => {});
    } else if (!on){
      fsApi = false;
      if (document.fullscreenElement) document.exitFullscreen();
      try { screen.orientation.unlock(); } catch(e){}
    }
  } catch(e){}
}
$('#fs').onclick = () => setFull(true);
$('#fsx').onclick = () => setFull(false);
$('#handle').onclick = () => {
  const open = document.body.classList.toggle('drawer');
  $('#handle').textContent = open ? '▸' : '◂';
};
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && fsApi) setFull(false); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') setFull(false); });

// ---------- статус ----------
async function status(){
  if (document.hidden || $('#app').hidden) return;
  try {
    const r = await fetch('/api/status', {credentials:'same-origin'});
    if (!r.ok) return; const s = await r.json();
    $('#st').innerHTML = `${s.host || ''}<br>CPU ${s.cpu ?? '-'}% · RAM ${s.ram ?? '-'}%` + (s.battery != null ? ` · 🔋${s.battery}%${s.plugged ? '⚡' : ''}` : '');
  } catch(e){ $('#st').textContent = 'немає зв’язку'; }
}
setInterval(status, 20000);
</script></body></html>"""

# --------------------------------------------------------------------------- #
#  Запуск і показ QR
# --------------------------------------------------------------------------- #
def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _open_file(path):
    try:
        if OS == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif OS == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def show_pairing(open_image=True):
    """Створює одноразовий код і показує QR ТІЛЬКИ на ноутбуці. Повертає текст для озвучення."""
    if USE_TUNNEL and not BASE["url"]:
        return "Інтернет-тунель ще піднімається. Спробуй за кілька секунд."
    base = BASE["url"] or f"http://{_lan_ip()}:{PORT}"
    code = new_pairing()
    link = f"{base}/#pair={code}"
    print("\n" + "=" * 62)
    print(" 📱 ПІДКЛЮЧЕННЯ ПРИСТРОЮ (одноразово, діє 5 хвилин)")
    print(f" Код: {code}")
    print(f" Адреса: {base}")
    print("=" * 62)
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(link)
        qr.print_ascii(invert=True)
        if STATE.get("gui"):
            qr.make(fit=True)
            STATE["qr"] = {"matrix": qr.get_matrix(), "until": time.time() + 300, "code": code}
        elif open_image:
            path = DATA / "pair_qr.png"
            qr.make_image().save(path)
            _open_file(path)
            threading.Timer(300, lambda: path.unlink(missing_ok=True)).start()
    except Exception as e:
        print(f"(QR недоступний: {e}) Введи код вручну на сторінці {base}")
    return "Показую QR-код на екрані. Відскануй його телефоном протягом п'яти хвилин."


def _on_tunnel_url(base, stable):
    BASE["url"], BASE["stable"] = base, stable
    (DATA / "remote_url.txt").write_text(base, encoding="utf-8")
    print(f"\n🌐 Orvex доступний з інтернету: {base}")
    # Постійна адреса й вже є пристрої -> нічого не показуємо. Інакше (перший запуск, або
    # адреса нова) -> одразу QR для підключення.
    if device_count() == 0 or not stable:
        show_pairing()


def start_server(orvex, port=PORT):
    app.config["ORVEX"] = orvex
    host = "0.0.0.0" if USE_LAN else "127.0.0.1"
    threading.Thread(
        target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
        daemon=True).start()

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps({"cloudflare_token": "", "public_url": ""}, indent=2),
                               encoding="utf-8")
    if USE_TUNNEL:
        from orvex_tunnel import start_tunnel
        start_tunnel(port, _on_tunnel_url)
    else:
        BASE["url"] = f"http://{_lan_ip()}:{port}"
        if device_count() == 0:
            show_pairing()
    return BASE["url"]

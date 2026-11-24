#!/usr/bin/env python3
"""ghostbox control daemon: runs inside the container, owns the virtual screen.

The container gives ghosthands a whole Linux desktop with no hardware: Xvfb is the monitor, a
real (headed, not headless) Chromium is the browser, and input goes in through the X server's
XTEST extension, so the browser receives ordinary trusted device events, the same path a USB
mouse takes once the kernel hands it to X. Motion follows curved, eased paths with human timing;
typing has per-key jitter.

HTTP API (one JSON object per POST /action, Content-Type application/json, BOX_TOKEN in X-Box-Token):
    {"verb": "status"}                          -> {"width", "height", "x", "y"}
    {"verb": "screenshot"}                      -> image/jpeg, X-Width / X-Height headers
    {"verb": "move", "x": px, "y": px}          -> human path from the current pointer
    {"verb": "click", "button": "left", "double": false}
    {"verb": "type", "text": "..."}
    {"verb": "key", "keys": "ctrl+l"}
    {"verb": "scroll", "amount": 3}             -> + = page down, in wheel notches
    {"verb": "navigate", "url": "https://..."}  -> ctrl+l, type, Return in the front browser
    {"verb": "vault"}                           -> {"logins": [{name, site, user, totp}]}  (no values)
    {"verb": "vault_import", "entries": {NAME: {url, user, value, totp?}}}   (from `ghostbox vault add` / `vault_import`)
    {"verb": "fill_secret", "name": NAME, "field": "username|password|totp", "x", "y"}
                                                -> types it only if the front tab is exactly
                                                   https://NAME's host and focus is the right input
    {"verb": "url"}                             -> {"url": the active tab's address, from the extension}
    {"verb": "save"}                            -> download what the active tab shows (a PDF)
    {"verb": "reset"}                           -> open a clean tab and close all prior tabs
    {"verb": "downloads"}                       -> {"done": [[name, bytes]], "pending": [name]}
Coordinates are screen pixels; the client converts ghosthands' 0..1 fractions.
"""
import base64
import hashlib
import hmac
import re
import struct
import io
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image
from Xlib import X, XK, display as xdisplay
from Xlib.ext import xtest

DISPLAY = os.environ.get("DISPLAY", ":99")
TOKEN = os.environ.get("BOX_TOKEN", "")
if len(TOKEN) < 16:
    sys.exit("boxd: BOX_TOKEN (16+ chars) is required; entrypoint.sh generates one")
PORT = int(os.environ.get("BOX_PORT", "8780"))
JPEG_Q = int(os.environ.get("BOX_JPEG_QUALITY", "85"))

_lock = threading.Lock()          # one input sequence at a time: a human has one hand on the mouse
_d = xdisplay.Display(DISPLAY)
_root = _d.screen().root
W, H = _d.screen().width_in_pixels, _d.screen().height_in_pixels


def _rand(a, b):
    return random.uniform(a, b)


# ---------------------------------------------------------------- pointer

def pointer():
    p = _root.query_pointer()
    return p.root_x, p.root_y


def _warp(x, y):
    xtest.fake_input(_d, X.MotionNotify, x=int(x), y=int(y))
    _d.sync()


def _skew_ease(u, skew=1.3):
    """Asymmetric minimum-jerk curve: fast initial acceleration (~32-40% peak), smooth deceleration."""
    u = max(0.0, min(1.0, float(u)))
    t = 1.0 - (1.0 - u) ** skew
    return t * t * t * (10.0 + t * (-15.0 + 6.0 * t))


def _scroll_gaps(n, pace=1.0):
    """Per-notch delays (seconds) for an eased scroll flick with periodic finger resets."""
    n = max(0, int(n))
    if n == 0:
        return []
    pace = max(0.2, min(12.0, float(pace)))
    gaps = []
    pos = 0
    while pos < n:
        rem = n - pos
        flick = rem if rem <= 6 else min(rem, random.randint(4, 6))
        fast = _rand(0.022, 0.030)
        slow = _rand(0.058, 0.092)
        for j in range(flick):
            d = abs(2.0 * j / (flick - 1) - 1.0) if flick > 1 else 1.0
            s = d * d * (3.0 - 2.0 * d)
            base = fast + s * (slow - fast)
            gap = max(0.012, (base + random.uniform(-0.003, 0.003)) * pace)
            if j == flick - 1 and pos + flick < n:
                gap += _rand(0.12, 0.26) * pace
            gaps.append(gap)
        pos += flick
    return gaps


def human_move(tx, ty):
    """Cubic Bezier from the current pointer to (tx, ty) with a sideways bow, asymmetric
    minimum-jerk easing, subtle correlated hand tremor, ~120 Hz jittered samples, and a curved
    overshoot-and-correct on long moves."""
    tx = max(0, min(W - 1, int(tx))); ty = max(0, min(H - 1, int(ty)))
    sx, sy = pointer()
    dist = math.hypot(tx - sx, ty - sy)
    if dist < 2:
        _warp(tx, ty); return
    dur = min(1.1, 0.12 + 0.11 * math.log2(1 + dist / 18.0)) * _rand(0.85, 1.2)
    # control points: pushed off the straight line by up to ~18% of the distance
    nx, ny = -(ty - sy) / dist, (tx - sx) / dist
    bow = dist * _rand(-0.18, 0.18)
    c1 = (sx + (tx - sx) * _rand(0.2, 0.4) + nx * bow, sy + (ty - sy) * _rand(0.2, 0.4) + ny * bow)
    c2 = (sx + (tx - sx) * _rand(0.6, 0.85) + nx * bow * 0.5, sy + (ty - sy) * _rand(0.6, 0.85) + ny * bow * 0.5)
    overshoot = dist > 300 and random.random() < 0.35
    ex, ey = (tx, ty)
    if overshoot:
        k = _rand(0.02, 0.05)
        ex, ey = tx + (tx - sx) * k, ty + (ty - sy) * k
    steps = max(8, int(dur * 120))
    skew = _rand(1.18, 1.45)
    amp = min(2.0, dist / 220.0) * 0.6
    jx = jy = 0.0
    last = None
    step_dt = dur / steps
    next_t = time.monotonic()
    for i in range(1, steps + 1):
        u = i / steps
        e = _skew_ease(u, skew)
        a, b, c, d = (1 - e) ** 3, 3 * (1 - e) ** 2 * e, 3 * (1 - e) * e ** 2, e ** 3
        env = math.sin(math.pi * u)
        jx = 0.7 * jx + random.gauss(0, amp)
        jy = 0.7 * jy + random.gauss(0, amp)
        x = a * sx + b * c1[0] + c * c2[0] + d * ex + jx * env
        y = a * sy + b * c1[1] + c * c2[1] + d * ey + jy * env
        pt = (max(0, min(W - 1, int(round(x)))), max(0, min(H - 1, int(round(y)))))
        if pt != last:
            _warp(*pt); last = pt
        # jittered pacing around the clock so a slow X server does not stretch the move
        next_t += step_dt * _rand(0.8, 1.2)
        lag = next_t - time.monotonic()
        if lag > 0:
            time.sleep(lag)
    if overshoot:
        time.sleep(_rand(0.04, 0.09))
        odx, ody = tx - ex, ty - ey
        odist = max(1.0, math.hypot(odx, ody))
        onx, ony = -ody / odist, odx / odist
        obow = odist * _rand(-0.22, 0.22)
        ocx = ex + odx * 0.5 + onx * obow
        ocy = ey + ody * 0.5 + ony * obow
        osteps = random.randint(6, 10)
        for i in range(1, osteps + 1):
            t = _skew_ease(i / osteps, 1.25)
            mt = 1.0 - t
            px = mt * mt * ex + 2 * mt * t * ocx + t * t * tx
            py = mt * mt * ey + 2 * mt * t * ocy + t * t * ty
            pt = (max(0, min(W - 1, int(round(px)))), max(0, min(H - 1, int(round(py)))))
            if pt != last:
                _warp(*pt); last = pt
            time.sleep(_rand(0.009, 0.022))
    if last != (tx, ty):
        _warp(tx, ty)


_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def click(button="left", double=False):
    b = _BUTTONS.get(button, 1)
    for n in range(2 if double else 1):
        xtest.fake_input(_d, X.ButtonPress, b); _d.sync()
        time.sleep(_rand(0.05, 0.12))
        if not double and random.random() < 0.25:
            dx, dy = random.choice((-1, 0, 1)), random.choice((-1, 0, 1))
            if dx or dy:
                px, py = pointer()
                _warp(max(0, min(W - 1, px + dx)), max(0, min(H - 1, py + dy)))
                time.sleep(_rand(0.008, 0.02))
        xtest.fake_input(_d, X.ButtonRelease, b); _d.sync()
        if double and n == 0:
            time.sleep(_rand(0.07, 0.13))


def scroll(amount):
    """Wheel notches: + = down (button 5), - = up (button 4). Accepts int or {amount, pace}."""
    pace = 1.0
    if isinstance(amount, dict):
        pace = float(amount.get("pace", 1.0))
        amount = amount.get("amount", 0)
    n = int(amount)
    if n == 0:
        return
    b = 5 if n > 0 else 4
    for gap in _scroll_gaps(min(abs(n), 40), pace=pace):
        xtest.fake_input(_d, X.ButtonPress, b); _d.sync()
        xtest.fake_input(_d, X.ButtonRelease, b); _d.sync()
        time.sleep(gap)


# ---------------------------------------------------------------- keyboard
# Typing and chords go through xdotool, which also speaks XTEST and handles keymap remapping
# for characters outside the current layout. Every call is an argv list: no shell.

# ghosthands key names (often written for a Mac) -> X keysym names. On this Linux browser the
# Mac "cmd" chords are "ctrl" chords; a Mac "delete" is BackSpace.
_KEYS = {"cmd": "ctrl", "command": "ctrl", "meta": "ctrl", "super": "ctrl", "control": "ctrl",
         "ctrl": "ctrl", "option": "alt", "alt": "alt", "shift": "shift",
         "return": "Return", "enter": "Return", "tab": "Tab", "escape": "Escape", "esc": "Escape",
         "delete": "BackSpace", "backspace": "BackSpace", "forwarddelete": "Delete", "del": "Delete",
         "space": "space", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
         "arrow-up": "Up", "arrow-down": "Down", "arrow-left": "Left", "arrow-right": "Right",
         "pageup": "Page_Up", "pagedown": "Page_Down", "pgup": "Page_Up", "pgdn": "Page_Down",
         "home": "Home", "end": "End"}


def keysym_chord(keys):
    if isinstance(keys, (list, tuple)):
        parts = [str(k) for k in keys]
    else:
        parts = str(keys).replace(",", "+").split("+")
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        k = _KEYS.get(p.lower())
        if k is None:
            k = p.upper() if (len(p) in (2, 3) and p[0] in "fF" and p[1:].isdigit()) else (p.lower() if len(p) == 1 else p)
        out.append(k)
    return "+".join(out)


def _xdo(*args, stdin=None):
    subprocess.run(["xdotool", *args], check=True, timeout=60, input=stdin, text=stdin is not None,
                   capture_output=True, env={**os.environ, "DISPLAY": DISPLAY})


_ASCII_KEYSYM = {
    " ": "space", "\n": "Return", "\t": "Tab",
    "!": "exclam", '"': "quotedbl", "#": "numbersign", "$": "dollar", "%": "percent",
    "&": "ampersand", "'": "apostrophe", "(": "parenleft", ")": "parenright",
    "*": "asterisk", "+": "plus", ",": "comma", "-": "minus", ".": "period",
    "/": "slash", ":": "colon", ";": "semicolon", "<": "less", "=": "equal",
    ">": "greater", "?": "question", "@": "at", "[": "bracketleft", "\\": "backslash",
    "]": "bracketright", "^": "asciicircum", "_": "underscore", "`": "grave",
    "{": "braceleft", "|": "bar", "}": "braceright", "~": "asciitilde",
}
_FAST_BIGRAMS = frozenset((
    "th", "he", "in", "er", "an", "re", "on", "en", "es", "st",
    "ed", "to", "it", "is", "ou", "ar", "te", "ng", "al", "se",
))
_KEYMAP_CACHE = {}
_SHIFT_KC = None


def _lognorm_clamp(med, sigma, lo, hi):
    return max(lo, min(hi, random.lognormvariate(math.log(med), sigma)))


def _type_delay(prev, ch):
    """Log-normal inter-key delay with bigram speedups, punctuation pauses, and short hesitations."""
    pair = (prev + ch).lower() if prev else ""
    if ch in " .,@/":
        base = _lognorm_clamp(0.115, 0.35, 0.050, 0.320)
    elif pair in _FAST_BIGRAMS:
        base = _lognorm_clamp(0.052, 0.30, 0.024, 0.145)
    elif prev and prev.islower() and ch.isupper():
        base = _lognorm_clamp(0.095, 0.30, 0.045, 0.240)
    else:
        base = _lognorm_clamp(0.078, 0.34, 0.032, 0.230)
    if random.random() < 0.05:
        base += _rand(0.10, 0.28)
    return base


def _char_key(ch):
    """Resolve an ASCII character to (keycode, needs_shift) via Xlib, or None."""
    if ch in _KEYMAP_CACHE:
        return _KEYMAP_CACHE[ch]
    res = None
    if ch in _ASCII_KEYSYM or (len(ch) == 1 and ch.isascii() and ch.isalnum()):
        name = _ASCII_KEYSYM.get(ch, ch)
        ks = XK.string_to_keysym(name)
        if ks:
            for kc, idx in _d.keysym_to_keycodes(ks):
                if idx in (0, 1):
                    res = (kc, idx == 1)
                    break
    _KEYMAP_CACHE[ch] = res
    return res


def key(keys):
    chord = keysym_chord(keys)
    if chord:
        _xdo("key", "--clearmodifiers", chord)


def type_text(text):
    """Direct XTEST keystrokes with human hold dwell and bigram rhythm; xdotool fallback only for non-ASCII."""
    global _SHIFT_KC
    if _SHIFT_KC is None:
        _SHIFT_KC = _d.keysym_to_keycode(XK.XK_Shift_L)
    prev = ""
    for i, ch in enumerate(text):
        mapped = _char_key(ch)
        if mapped and (_SHIFT_KC or not mapped[1]):
            kc, need_shift = mapped
            if need_shift:
                xtest.fake_input(_d, X.KeyPress, _SHIFT_KC); _d.sync()
                time.sleep(_rand(0.014, 0.042))
            try:
                xtest.fake_input(_d, X.KeyPress, kc); _d.sync()
                time.sleep(_lognorm_clamp(0.062, 0.28, 0.025, 0.130))
                xtest.fake_input(_d, X.KeyRelease, kc); _d.sync()
            finally:
                if need_shift:
                    time.sleep(_rand(0.012, 0.038))
                    xtest.fake_input(_d, X.KeyRelease, _SHIFT_KC); _d.sync()
        else:
            # fallback for non-ASCII / unmapped keysyms; passed on stdin, never argv
            _xdo("type", "--delay", "0", "--file", "-", stdin=ch)
        if i + 1 < len(text):
            time.sleep(_type_delay(prev, ch))
        prev = ch


_ext = None


def ext():
    """The vault extension link (box/extlink.py), started with boxd."""
    global _ext
    if _ext is None:
        from extlink import ExtLink
        _ext = ExtLink()
    return _ext


def front_tab():
    """Origin, URL and focused element of the active tab, as the vault extension sees them from
    its isolated world. Keystrokes and the clipboard are not used: a page can intercept both."""
    return ext().call("front")


def current_url():
    return front_tab().get("href", "")


def check_url(url):
    """Only plain web addresses: a typed javascript: URL runs in the open site, file: and
    chrome: reach the box itself."""
    from urllib.parse import urlparse
    u = str(url).strip()
    if any(ord(c) < 32 for c in u) or urlparse(u).scheme not in ("http", "https") or not urlparse(u).hostname:
        raise PermissionError("refused: navigate takes an http(s) address, got %r" % u[:80])
    return u


def navigate(url):
    """Open an http(s) address in the active tab through the vault extension (like a bookmark),
    not Ctrl+L and typing: a page can swallow Ctrl+L and keep the agent on it, and the address
    bar's inline autocompletion can turn a typed URL into a different one."""
    ext().call("navigate", url=check_url(url))


# ---------------------------------------------------------------- vault
# Logins the owner gave this box (`ghostbox vault add` or `vault_import`). Only the ghost user
# can read the file; the browser user cannot. Values never leave this process: the planner sees
# names, and a value is typed only when the front tab is on the login's own site.

VAULT = os.environ.get("BOX_VAULT", "/data/vault/vault.json")
# Height of Chromium's tab strip + toolbar at the box's 96 dpi; nothing secret is typed above it.
TOOLBAR_PX = int(os.environ.get("BOX_TOOLBAR_PX", "88"))


def _vault():
    try:
        with open(VAULT) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _host(url):
    from urllib.parse import urlparse
    h = (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def vault_list():
    return [{"name": n, "site": _host(e.get("url", "")), "user": e.get("user", ""),
             "totp": bool(e.get("totp"))} for n, e in sorted(_vault().items())]


def vault_import(entries):
    data = _vault()
    for n, e in entries.items():
        if not (isinstance(n, str) and n.startswith("PW_") and isinstance(e, dict) and e.get("url")):
            raise ValueError("bad vault entry %r" % n)
        data[n] = {k: str(e[k]) for k in ("url", "user", "value", "totp") if e.get(k)}
    _save_vault(data)
    return sorted(entries)


def vault_remove(names):
    data = _vault()
    gone = [n for n in names if data.pop(n, None) is not None]
    if gone:
        _save_vault(data)
    return gone


def _save_vault(data):
    os.makedirs(os.path.dirname(VAULT), mode=0o700, exist_ok=True)
    tmp = VAULT + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, VAULT)


def _check_front_input(want, field):
    """Verify the front tab's origin, focus, and writable input type via the isolated extension."""
    r = ext().call("front")
    ok = (
        isinstance(r, dict)
        and r.get("origin") == want
        and r.get("focused") is True
        and r.get("visible") is True
        and r.get("tag") == "INPUT"
        and not r.get("readOnly")
        and not r.get("inFrame")
        and str(r.get("type") or "").lower() in _FIELD_TYPES[field]
    )
    if not ok:
        r = r or {}
        if r.get("origin") != want:
            raise PermissionError("refused: the page is %s, not %s" % (r.get("origin"), want))
        raise PermissionError("refused: focus is on %s type=%s" % (r.get("tag"), r.get("type")))
    return r


def totp(secret, t=None, digits=6, step=30):
    """RFC 6238 code from a base32 secret (what an authenticator app holds)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    msg = struct.pack(">Q", int((time.time() if t is None else t) // step))
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


_FIELD_TYPES = {"password": {"password"}, "username": {"text", "email", "tel"},
                "totp": {"text", "tel", "number", "password"}}


def fill_secret(name, field, x=None, y=None):
    """Click the field at x,y, then put a vault login's username, password or current TOTP code
    into it. Default mode (`BOX_FILL_MODE=ext`) checks and fills atomically in the vault extension's
    isolated world. `BOX_FILL_MODE=type` checks front_tab() before every 4-char XTEST burst and fails
    closed if origin or focus changes. Errors never carry the value."""
    e = _vault().get(name)
    if not e:
        raise ValueError("no login %s in this box's vault (host: ghostbox vault add %s ...)" % (name, name))
    if field in ("otp", "code"):
        field = "totp"
    if field not in _FIELD_TYPES:
        raise ValueError("field must be username, password or totp")
    if field == "totp" and not e.get("totp"):
        raise ValueError("%s has no authenticator secret; this sign-in needs the owner (push or SMS)" % name)
    if x is None or y is None:
        raise ValueError("fill_secret needs the field's position")
    if int(y) < TOOLBAR_PX:
        raise PermissionError("refused: that point is in the browser toolbar, not the page")
    if field == "totp" and 30 - time.time() % 30 < 4:
        time.sleep(30 - time.time() % 30 + 0.5)      # code about to roll over: wait before checking
    human_move(x, y); time.sleep(_rand(0.05, 0.12)); click()
    time.sleep(_rand(0.2, 0.35))
    if field == "totp":
        value = totp(e["totp"])
    else:
        value = e.get("value") if field == "password" else e.get("user")
    if not value:
        raise ValueError("%s has no %s" % (name, field))
    want = "https://" + _host(e["url"])
    if os.environ.get("BOX_FILL_MODE", "ext").strip().lower() == "type":
        for i in range(0, len(value), 4):
            _check_front_input(want, field)
            type_text(value[i:i + 4])
        return want
    try:
        r = ext().call("fill", origin=want, types=sorted(_FIELD_TYPES[field]), value=value)
    except Exception:
        raise RuntimeError("putting the secret in failed") from None
    if not (r or {}).get("ok"):
        r = r or {}
        if r.get("origin") != want:
            raise PermissionError("refused: the page is %s, not %s" % (r.get("origin"), want))
        raise PermissionError("refused: focus is on %s type=%s" % (r.get("tag"), r.get("type")))
    return r.get("origin")


# ---------------------------------------------------------------- downloads

DOWNLOADS = os.environ.get("BOX_DOWNLOADS_DIR", "/data/downloads")


def downloads():
    """Finished downloads (name, bytes) and names still in progress. No links followed."""
    done, pending = [], []
    try:
        for e in os.scandir(DOWNLOADS):
            if not e.is_file(follow_symlinks=False):
                continue
            if e.name.endswith(".crdownload"):
                pending.append(e.name[:-len(".crdownload")])
            else:
                done.append([e.name, e.stat(follow_symlinks=False).st_size])
    except OSError:
        pass
    return {"done": sorted(done), "pending": sorted(pending)}


# ---------------------------------------------------------------- screen

def screenshot():
    raw = _root.get_image(0, 0, W, H, X.ZPixmap, 0xFFFFFFFF)
    img = Image.frombytes("RGB", (W, H), raw.data, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_Q)
    return buf.getvalue()


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "ghostbox/1"

    def log_message(self, fmt, *a):
        sys.stderr.write("boxd %s\n" % (fmt % a))

    def _reply(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True, "width": W, "height": H})
        self._reply(404, {"error": "POST /action"})

    def do_POST(self):
        # The box's own browser can reach this port. A page can send a "simple" cross-origin POST
        # without a preflight; such a request carries an Origin header or a non-JSON body, and
        # neither reaches an action. The token is the real gate.
        if self.headers.get("Origin") or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._reply(403, {"error": "API clients only"})
        if not hmac.compare_digest(self.headers.get("X-Box-Token") or "", TOKEN):
            return self._reply(401, {"error": "bad token"})
        if self.path != "/action":
            return self._reply(404, {"error": "POST /action"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            verb = req.get("verb")
            with _lock:
                if verb == "status":
                    x, y = pointer()
                    return self._reply(200, {"width": W, "height": H, "x": x, "y": y})
                if verb == "vault":
                    return self._reply(200, {"logins": vault_list()})
                if verb == "vault_import":
                    return self._reply(200, {"imported": vault_import(req.get("entries") or {})})
                if verb == "vault_remove":
                    return self._reply(200, {"removed": vault_remove(req.get("names") or [])})
                if verb == "fill_secret":
                    here = fill_secret(str(req.get("name", "")), str(req.get("field", "password")),
                                       req.get("x"), req.get("y"))
                    return self._reply(200, {"ok": True, "site": here})
                if verb == "save":
                    return self._reply(200, ext().call("save"))
                if verb == "reset":
                    return self._reply(200, ext().call("reset"))
                if verb == "url":
                    return self._reply(200, {"url": current_url()})
                if verb == "downloads":
                    return self._reply(200, downloads())
                if verb == "screenshot":
                    return self._reply(200, screenshot(), "image/jpeg", {"X-Width": W, "X-Height": H})
                if verb == "move":
                    human_move(req["x"], req["y"])
                elif verb == "click":
                    if "x" in req and "y" in req:
                        human_move(req["x"], req["y"]); time.sleep(_rand(0.04, 0.12))
                    click(req.get("button", "left"), bool(req.get("double")))
                elif verb == "type":
                    type_text(str(req.get("text", "")))
                elif verb == "key":
                    key(req.get("keys", ""))
                elif verb == "scroll":
                    scroll(req.get("amount", 0))
                elif verb == "navigate":
                    navigate(str(req.get("url", "")))
                else:
                    return self._reply(400, {"error": "unknown verb %r" % verb})
            self._reply(200, {"ok": True})
        except PermissionError as e:
            self._reply(403, {"error": str(e)})
        except Exception as e:  # report, keep serving (messages never carry a vault value)
            self._reply(500, {"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    ext()                                   # start listening for the vault extension
    print("boxd on :%d, screen %dx%d, display %s" % (PORT, W, H, DISPLAY), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

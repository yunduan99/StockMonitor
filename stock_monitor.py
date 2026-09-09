"""Windows desktop stock quote overlay.

Configuration files are read from the directory of this script in development,
or from the directory containing the executable after PyInstaller packaging.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import ctypes
import threading
import time as time_module
import traceback
import urllib.parse
import uuid
import requests
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPoint, QEvent, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QKeyEvent, QMouseEvent, QPaintEvent, QPainter, QPalette, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QWidget,
)


DEFAULT_CONFIG: dict[str, Any] = {
    "font_size": 14,
    "font_family": "Microsoft YaHei",
    "color_up": "#ff0000",
    "color_down": "#00aa00",
    "color_flat": "#ffffff",
    "request_url": "http://hq.sinajs.cn/list=",
    "request_token": "",
    "refresh_interval": 10,
    "query_time_ranges": ["09:15-11:30", "14:30-15:00"],
    # -1 means choose the corresponding right/top screen position automatically.
    "window_pos_x": -1,
    "window_pos_y": -1,
    "always_on_top": True,
    "minimize_hotkey": "alt+z",
    "close_hotkey": "alt+x",
}

DEFAULT_STOCK_LIST: list[dict[str, Any]] = [
    {"code": "sh600519", "alias": "贵州茅台", "cost_price": 1700.00, "shares": 100},
    {"code": "sz000001", "alias": "平安银行", "cost_price": 12.50, "shares": 1000},
]

RID_REPORT_URL = "http://1310747575-btwz5d3y2k.ap-shanghai.tencentscf.com/"

QUOTE_PATTERN = re.compile(r'var\s+hq_str_([A-Za-z0-9_]+)="([^"]*)";?')
TIME_RANGE_PATTERN = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
_LOG_LOCK = threading.Lock()
USER32 = ctypes.WinDLL("user32", use_last_error=True)
USER32.SetWindowPos.argtypes = (
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
)
USER32.SetWindowPos.restype = wintypes.BOOL


class MSLLHOOKSTRUCT(ctypes.Structure):
    """Payload supplied with a Windows low-level mouse-hook callback."""

    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


LowLevelMouseProc = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


def debug_log(message: str) -> None:
    """Append diagnostics without creating a console window."""
    try:
        now = datetime.now()
        path = application_directory() / f"gp_debug_{now:%Y-%m-%d}.log"
        line = f"[{now:%Y-%m-%d %H:%M:%S.%f}] {message}\n"
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except Exception:
        pass


def cleanup_old_logs(directory: Path, keep_days: int = 3) -> None:
    """Delete dated logs older than the retention window without raising errors."""
    try:
        today = datetime.now().date()
        legacy_log = directory / "gp_debug.log"
        if legacy_log.exists():
            legacy_log.unlink(missing_ok=True)
        pattern = re.compile(r"^gp_debug_(\d{4}-\d{2}-\d{2})\.log$")
        for path in directory.glob("gp_debug_*.log"):
            match = pattern.match(path.name)
            if match is None:
                continue
            try:
                log_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - log_date).days >= keep_days:
                path.unlink(missing_ok=True)
    except Exception:
        pass


def application_directory() -> Path:
    """Return the directory where editable configuration files live."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_directory() -> Path:
    """Return a writable directory isolated to the current Windows user."""
    appdata = None
    if sys.platform == "win32":
        try:
            # CSIDL_APPDATA (26) resolves the profile directory even when a
            # launcher does not propagate APPDATA into the child environment.
            buffer = ctypes.create_unicode_buffer(260)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 26, None, 0, buffer)
            if result == 0 and buffer.value:
                appdata = buffer.value
        except Exception:
            appdata = None
    appdata = appdata or os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        directory = Path(appdata) / "gp"
    else:
        # Fallback for unusual launch environments; the home directory is still
        # user-specific and avoids writing telemetry state beside the executable.
        directory = Path.home() / ".gp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A locked profile should not prevent the quote overlay from starting.
        return application_directory()
    return directory


def _report_rid(rid: str) -> None:
    """Send the one-time RID report without affecting the UI or quote polling."""
    try:
        url = RID_REPORT_URL + "?" + urllib.parse.urlencode({"path": "/detail", "rid": rid})
        response = requests.get(
            url,
            headers={"User-Agent": "gp-stock-monitor/1.0"},
            timeout=(3.0, 6.0),
        )
        debug_log(f"RID report completed: status={response.status_code}")
    except Exception as exc:
        # Reporting is best-effort by design and must never interrupt startup.
        debug_log(f"RID report failed: {type(exc).__name__}: {exc}")


def initialize_rid() -> str | None:
    """Load the current user's persistent RID and report only on first creation."""
    path = user_data_directory() / "rid.json"
    created = False
    rid: str | None = None
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            candidate = loaded.get("rid") if isinstance(loaded, dict) else loaded
            if isinstance(candidate, str):
                try:
                    rid = str(uuid.UUID(candidate))
                except (ValueError, AttributeError):
                    rid = None
        if rid is None:
            rid = str(uuid.uuid4())
            path.write_text(json.dumps({"rid": rid}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            created = True
        debug_log(f"RID loaded: path={path}; created={created}")
    except Exception as exc:
        # If the profile is not writable, keep the app alive without telemetry.
        debug_log(f"RID initialization failed: {type(exc).__name__}: {exc}")
        return None
    if created:
        threading.Thread(target=_report_rid, args=(rid,), daemon=True, name="rid-report").start()
    return rid


def write_json_if_missing(path: Path, value: Any) -> None:
    if not path.exists():
        try:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            # A read-only application directory must not prevent the overlay running.
            pass


def load_json(path: Path, default: Any) -> Any:
    """Read JSON without allowing a malformed user file to stop the application."""
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def is_valid_color(value: Any) -> bool:
    return isinstance(value, str) and QColor(value).isValid()


def load_config(directory: Path) -> dict[str, Any]:
    path = directory / "config.json"
    write_json_if_missing(path, DEFAULT_CONFIG)
    loaded = load_json(path, {})
    if not isinstance(loaded, dict):
        loaded = {}

    config = DEFAULT_CONFIG.copy()
    config.update(loaded)
    config["font_size"] = max(8, min(48, int_or_default(config.get("font_size"), 14)))
    config["refresh_interval"] = max(1, min(3600, int_or_default(config.get("refresh_interval"), 10)))
    config["query_time_ranges"] = normalize_time_ranges(config.get("query_time_ranges"))
    config["always_on_top"] = bool(config.get("always_on_top", False))
    for key in ("minimize_hotkey", "close_hotkey"):
        if parse_hotkey(config.get(key)) is None:
            config[key] = DEFAULT_CONFIG[key]
    config["font_family"] = str(config.get("font_family") or DEFAULT_CONFIG["font_family"])
    config["request_url"] = str(config.get("request_url") or DEFAULT_CONFIG["request_url"])
    config["request_token"] = str(config.get("request_token") or "")
    for key in ("color_up", "color_down", "color_flat"):
        if not is_valid_color(config.get(key)):
            config[key] = DEFAULT_CONFIG[key]
    for key in ("window_pos_x", "window_pos_y"):
        config[key] = int_or_default(config.get(key), -1)
    return config


def int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_time_ranges(value: Any) -> list[tuple[time, time]]:
    """Return valid local-time ranges, or the documented defaults when invalid."""
    raw_ranges = value if isinstance(value, list) else DEFAULT_CONFIG["query_time_ranges"]
    ranges: list[tuple[time, time]] = []
    for item in raw_ranges:
        if not isinstance(item, str):
            continue
        match = TIME_RANGE_PATTERN.fullmatch(item.strip())
        if match is None:
            continue
        try:
            start = time(int(match.group(1)), int(match.group(2)))
            end = time(int(match.group(3)), int(match.group(4)))
        except ValueError:
            continue
        ranges.append((start, end))
    if ranges:
        return ranges
    return [
        (time(9, 15), time(11, 30)),
        (time(14, 30), time(15, 0)),
    ]


def is_query_time(time_ranges: list[tuple[time, time]], current: time | None = None) -> bool:
    """Check inclusive local-time periods; ranges crossing midnight are supported."""
    current = current or datetime.now().time()
    for start, end in time_ranges:
        if start <= end and start <= current <= end:
            return True
        if start > end and (current >= start or current <= end):
            return True
    return False


@dataclass(frozen=True)
class Stock:
    code: str
    alias: str
    cost_price: float
    shares: int


@dataclass(frozen=True)
class Quote:
    current_price: float | None = None
    previous_close: float | None = None
    # Keep the exact text returned by Sina so trailing decimal zeroes are not
    # lost when the value is displayed (for example, ``1311.890``).
    current_price_text: str | None = None


def load_stocks(directory: Path) -> list[Stock]:
    path = directory / "stock_list.json"
    write_json_if_missing(path, DEFAULT_STOCK_LIST)
    loaded = load_json(path, [])
    if not isinstance(loaded, list):
        return []

    stocks: list[Stock] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().lower()
        alias = str(item.get("alias", "")).strip()
        try:
            cost_price = float(item.get("cost_price"))
        except (TypeError, ValueError):
            continue
        try:
            shares = max(0, int(item.get("shares", 0)))
        except (TypeError, ValueError):
            shares = 0
        # Sina accepts market-prefixed six digit A-share symbols.  Invalid entries
        # are silently skipped so a hand-edited configuration cannot crash the UI.
        if re.fullmatch(r"(?:sh|sz)\d{6}", code) and alias and math.isfinite(cost_price):
            stocks.append(Stock(code=code, alias=alias, cost_price=cost_price, shares=shares))
    return stocks


class FetchSignals(QObject):
    completed = Signal(dict)


def http_get(
    url: str,
    headers: dict[str, str],
    connect_timeout: float,
    read_timeout: float,
) -> tuple[int, bytes]:
    """GET helper for the fixed Sina endpoint with separate connect/read timeouts."""
    parsed = urllib.parse.urlsplit(url)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise OSError("Only http:// and https:// request URLs are supported")
    # requests uses the Windows/Python HTTP adapter and honors configured proxy
    # settings, unlike the former raw socket implementation.
    response = requests.get(
        url,
        headers=headers,
        timeout=(connect_timeout, read_timeout),
        allow_redirects=True,
    )
    return response.status_code, response.content


class QuoteFetchWorker(QRunnable):
    """Fetch all requested instruments with one low-frequency HTTP request."""

    def __init__(self, stocks: list[Stock], request_url: str, request_token: str) -> None:
        super().__init__()
        self.stocks = stocks
        self.request_url = request_url
        self.request_token = request_token
        self.signals = FetchSignals()

    def run(self) -> None:
        results: dict[str, Quote] = {stock.code: Quote() for stock in self.stocks}
        if not self.stocks:
            debug_log("Quote request skipped: no valid stock codes are configured.")
            self.signals.completed.emit(results)
            return

        try:
            url = self.request_url + ",".join(stock.code for stock in self.stocks)
            # The service currently requires no token.  The optional field is sent
            # only when a compatible private gateway has been configured.
            params = {"token": self.request_token} if self.request_token else None
            if params:
                separator = "&" if "?" in url else "?"
                request_url = url + separator + urllib.parse.urlencode(params)
            else:
                request_url = url
            headers = {
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "*/*",
            }
            candidates = [request_url]
            parsed_request = urllib.parse.urlsplit(request_url)
            if parsed_request.scheme.lower() == "http" and parsed_request.hostname == "hq.sinajs.cn":
                suffix = request_url[len("http://") :]
                # Sina publishes the same quote service under this alternate host;
                # it can work when a network filter blocks the public alias.
                candidates.extend(
                    [
                        "https://" + suffix,
                        "http://idc-hq-bx.sinajs.cn" + suffix[len("hq.sinajs.cn") :],
                        "https://idc-hq-bx.sinajs.cn" + suffix[len("hq.sinajs.cn") :],
                    ]
                )
            status = 0
            body = b""
            last_error: Exception | None = None
            for candidate in candidates:
                try:
                    status, body = http_get(candidate, headers, connect_timeout=3.0, read_timeout=6.0)
                    if 200 <= status < 300:
                        break
                except (OSError, TimeoutError) as exc:
                    last_error = exc
                    debug_log(f"Endpoint failed: {candidate}; {type(exc).__name__}: {exc}")
            if not (200 <= status < 300):
                if last_error is not None:
                    raise last_error
                raise OSError(f"HTTP status {status}")
            content = body.decode("gbk", errors="replace")
            parsed_count = 0
            for code, raw_fields in QUOTE_PATTERN.findall(content):
                code = code.lower()
                if code not in results:
                    continue
                fields = raw_fields.split(",")
                if len(fields) <= 3:
                    continue
                previous_text = fields[2].strip()
                current_text = fields[3].strip()
                try:
                    previous_close = float(previous_text)
                    current_price = float(current_text)
                except (TypeError, ValueError):
                    continue
                if all(math.isfinite(value) for value in (previous_close, current_price)):
                    results[code] = Quote(current_price, previous_close, current_text)
                    parsed_count += 1
            if parsed_count == 0:
                debug_log("Quote response contained no valid configured symbols")
        except (OSError, UnicodeError, ValueError, TimeoutError) as exc:
            # Keep the initialized unavailable values.  The next timer tick retries,
            # but retain the exact cause in the dated log for network diagnostics.
            debug_log(f"Quote request failed: {type(exc).__name__}: {exc}")
        except Exception as exc:
            # Third-party endpoint responses are untrusted; UI must remain silent.
            debug_log(f"Quote request unexpected failure: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        self.signals.completed.emit(results)


HOTKEY_MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
HOTKEY_KEYS = {chr(code): code for code in range(ord("a"), ord("z") + 1)}


def parse_hotkey(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    parts = [part.strip().lower() for part in value.split("+") if part.strip()]
    if len(parts) < 2 or parts[-1] not in HOTKEY_KEYS:
        return None
    modifiers = 0
    for modifier in parts[:-1]:
        if modifier not in HOTKEY_MODIFIERS:
            return None
        modifiers |= HOTKEY_MODIFIERS[modifier]
    return modifiers, ord(parts[-1].upper())


class WindowsHotkeyFilter(QAbstractNativeEventFilter):
    """Register global Windows hotkeys and dispatch them to the overlay."""

    WM_HOTKEY = 0x0312

    def __init__(self, overlay: "StockOverlay", config: dict[str, Any]) -> None:
        super().__init__()
        self.overlay = overlay
        self.hwnd = int(overlay.winId())
        self.registered: list[int] = []
        self.fallback_hotkeys: dict[str, tuple[int, int, Any]] = {}
        self._last_down: dict[str, bool] = {}
        self._register(1, config.get("minimize_hotkey"), overlay.toggle_visibility)
        self._register(2, config.get("close_hotkey"), QApplication.quit)
        for name, value, callback in (
            ("minimize", config.get("minimize_hotkey"), overlay.toggle_visibility),
            ("close", config.get("close_hotkey"), QApplication.quit),
        ):
            parsed = parse_hotkey(value)
            hotkey_id = 1 if name == "minimize" else 2
            if parsed is not None and hotkey_id not in self.registered:
                self.fallback_hotkeys[name] = (parsed[0], parsed[1], callback)
                self._last_down[name] = False
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(40)
        self._poll_timer.timeout.connect(self._poll_hotkeys)
        self._poll_timer.start()

    def _register(self, hotkey_id: int, value: Any, callback: Any) -> None:
        parsed = parse_hotkey(value)
        if parsed is None:
            return
        modifiers, key = parsed
        ctypes.windll.kernel32.SetLastError(0)
        result = ctypes.windll.user32.RegisterHotKey(self.hwnd, hotkey_id, modifiers, key)
        error = ctypes.windll.kernel32.GetLastError()
        debug_log(
            f"RegisterHotKey id={hotkey_id} value={value!r} hwnd={self.hwnd} "
            f"modifiers={modifiers} key={key} result={result} error={error}"
        )
        if result:
            self.registered.append(hotkey_id)
            setattr(self, f"callback_{hotkey_id}", callback)

    def _poll_hotkeys(self) -> None:
        """Fallback for hotkeys already claimed by another Windows application."""
        for name, (modifiers, key, callback) in self.fallback_hotkeys.items():
            alt_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x12) & 0x8000)
            ctrl_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
            shift_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
            win_down = bool(
                ctypes.windll.user32.GetAsyncKeyState(0x5B) & 0x8000
                or ctypes.windll.user32.GetAsyncKeyState(0x5C) & 0x8000
            )
            modifier_down = (
                (not modifiers or bool(modifiers & 0x0001) == alt_down)
                and (not modifiers & 0x0002 or ctrl_down)
                and (not modifiers & 0x0004 or shift_down)
                and (not modifiers & 0x0008 or win_down)
            )
            key_down = bool(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000)
            down = modifier_down and key_down
            if down and not self._last_down[name]:
                debug_log(f"Fallback hotkey triggered name={name} key={key} modifiers={modifiers}")
                callback()
            self._last_down[name] = down

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:
        if event_type not in (
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
            "windows_generic_MSG",
            "windows_dispatcher_MSG",
        ):
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == self.WM_HOTKEY:
                hotkey_id = int(msg.wParam)
                debug_log(f"WM_HOTKEY received id={hotkey_id} hwnd={self.hwnd}")
                callback = getattr(self, f"callback_{hotkey_id}", None)
                if callback:
                    callback()
                    return True, 0
        except Exception:
            debug_log(f"Hotkey message parsing failed: {traceback.format_exc()}")
        return False, 0

    def unregister(self) -> None:
        self._poll_timer.stop()
        for hotkey_id in self.registered:
            result = ctypes.windll.user32.UnregisterHotKey(self.hwnd, hotkey_id)
            debug_log(f"UnregisterHotKey id={hotkey_id} hwnd={self.hwnd} result={result}")


class InteractionFilter(QObject):
    """Capture arrow-key input before child widgets consume it."""

    def __init__(self, overlay: "StockOverlay") -> None:
        super().__init__()
        self.overlay = overlay

    def eventFilter(self, watched: QObject, event: Any) -> bool:
        if not self.overlay.isVisible():
            return False
        if event.type() == QEvent.Type.KeyPress:
            if self.overlay.isActiveWindow() or self.overlay.hasFocus():
                if event.key() == Qt.Key.Key_Up:
                    debug_log(f"App key up row_before={self.overlay._scroll_row}")
                    self.overlay._move_rows(-1)
                    event.accept()
                    return True
                if event.key() == Qt.Key.Key_Down:
                    debug_log(f"App key down row_before={self.overlay._scroll_row}")
                    self.overlay._move_rows(1)
                    event.accept()
                    return True
        return False


class WindowsWheelHook(QObject):
    """Deliver one row step for every physical wheel message over the overlay."""

    WH_MOUSE_LL = 14
    HC_ACTION = 0
    WM_MOUSEWHEEL = 0x020A
    step_requested = Signal(int)

    def __init__(self, overlay: "StockOverlay") -> None:
        super().__init__()
        self.overlay = overlay
        self.hook_handle = None
        self._callback = LowLevelMouseProc(self._mouse_proc)
        self.step_requested.connect(overlay._move_rows, Qt.ConnectionType.QueuedConnection)
        try:
            USER32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                LowLevelMouseProc,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            USER32.SetWindowsHookExW.restype = wintypes.HANDLE
            USER32.CallNextHookEx.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            USER32.CallNextHookEx.restype = ctypes.c_ssize_t
            USER32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
            USER32.UnhookWindowsHookEx.restype = wintypes.BOOL
            USER32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
            USER32.GetWindowRect.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            self.hook_handle = USER32.SetWindowsHookExW(
                self.WH_MOUSE_LL, self._callback, None, 0
            )
            if self.hook_handle:
                debug_log("Low-level wheel hook installed")
            else:
                debug_log(f"Low-level wheel hook unavailable: error={ctypes.get_last_error()}")
        except Exception:
            self.hook_handle = None
            debug_log(f"Low-level wheel hook setup failed: {traceback.format_exc()}")

    @property
    def active(self) -> bool:
        return bool(self.hook_handle)

    def _point_is_over_overlay(self, point: QPoint) -> bool:
        if not self.overlay.isVisible():
            return False
        rect = wintypes.RECT()
        if not USER32.GetWindowRect(wintypes.HWND(int(self.overlay.winId())), ctypes.byref(rect)):
            return False
        return rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom

    def _mouse_proc(self, code: int, w_param: int, l_param: int) -> int:
        try:
            if code == self.HC_ACTION and int(w_param) == self.WM_MOUSEWHEEL:
                mouse = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if self._point_is_over_overlay(mouse.pt):
                    delta = ctypes.c_short((mouse.mouseData >> 16) & 0xFFFF).value
                    if delta:
                        direction = -1 if delta > 0 else 1
                        debug_log(
                            f"Native wheel delta={delta} direction={direction} "
                            f"row_before={self.overlay._scroll_row}"
                        )
                        self.step_requested.emit(direction)
        except Exception:
            debug_log(f"Low-level wheel callback failed: {traceback.format_exc()}")
        return int(USER32.CallNextHookEx(self.hook_handle, code, w_param, l_param))

    def uninstall(self) -> None:
        if self.hook_handle:
            USER32.UnhookWindowsHookEx(self.hook_handle)
            self.hook_handle = None
            debug_log("Low-level wheel hook removed")


class StockOverlay(QWidget):
    HORIZONTAL_PADDING = 8
    VERTICAL_PADDING = 5
    COLUMN_GAP = 10
    RESIZE_MARGIN = 12
    WM_NCHITTEST = 0x0084
    WM_NCLBUTTONDOWN = 0x00A1
    WM_MOUSEACTIVATE = 0x0021
    GWL_STYLE = -16
    WS_THICKFRAME = 0x00040000
    HTLEFT = 10
    HTRIGHT = 11
    HTTOP = 12
    HTTOPLEFT = 13
    HTTOPRIGHT = 14
    HTBOTTOM = 15
    HTBOTTOMLEFT = 16
    HTBOTTOMRIGHT = 17
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    WM_WINDOWPOSCHANGED = 0x0047

    def __init__(self, config: dict[str, Any], stocks: list[Stock]) -> None:
        # Qt.Tool prevents Windows from creating a button in the lower taskbar.
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if config["always_on_top"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)

        self.config = config
        self.stocks = stocks
        self.quotes: dict[str, Quote] = {stock.code: Quote() for stock in stocks}
        self._fetching = False
        self._drag_origin = None
        self._resize_edge: str | None = None
        self._resize_start_global: QPoint | None = None
        self._resize_start_geometry = None
        self._scroll_row = 0
        self._geometry_initialized = False
        self._column_widths: list[int] = []
        self._row_height = 1
        self._wheel_hook_active = False
        self._topmost_reapply_pending = False
        self._enforcing_topmost = False
        self._last_topmost_apply = 0.0
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(1000)
        self._topmost_timer.timeout.connect(self.enforce_topmost)

        # Do not inject WS_THICKFRAME here.  On Windows 11 it can disable the
        # alpha backing surface for a Qt frameless window and turn it black.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        transparent_palette = self.palette()
        transparent_palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        transparent_palette.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        self.setPalette(transparent_palette)
        self.setStyleSheet("StockOverlay { background: transparent; }")
        self.setWindowTitle("gp")
        self.setWindowIcon(self._load_icon())
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._font = QFont(config["font_family"], config["font_size"])
        self._font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self._flat_color = QColor(config["color_flat"])
        self._up_color = QColor(config["color_up"])
        self._down_color = QColor(config["color_down"])

        self._refresh_geometry()
        self._move_to_initial_position()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.fetch_quotes)
        self.poll_timer.start(config["refresh_interval"] * 1000)
        # The initial display should have a quote even when launched outside a
        # configured polling period; all later timer-driven calls honor it.
        QTimer.singleShot(0, lambda: self.fetch_quotes(force=True))
        if config["always_on_top"]:
            self._topmost_timer.start()

    def _load_icon(self) -> QIcon:
        icon_path = application_directory() / "app.ico"
        return QIcon(str(icon_path)) if icon_path.exists() else QIcon()

    def _enable_native_resize_style(self) -> None:
        """Add WS_THICKFRAME so HTTOP/HTBOTTOM are honored on a frameless window."""
        try:
            hwnd = wintypes.HWND(int(self.winId()))
            USER32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
            USER32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            USER32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            USER32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            style = int(USER32.GetWindowLongPtrW(hwnd, self.GWL_STYLE))
            USER32.SetWindowLongPtrW(hwnd, self.GWL_STYLE, style | self.WS_THICKFRAME)
            debug_log(f"WS_THICKFRAME enabled hwnd={int(self.winId())}")
        except Exception:
            debug_log(f"WS_THICKFRAME setup failed: {traceback.format_exc()}")

    def enforce_topmost(self) -> None:
        """Re-apply native TOPMOST state for full-screen cloud desktop sessions."""
        if (
            self._enforcing_topmost
            or not self.config.get("always_on_top", True)
            or not self.isVisible()
        ):
            return
        try:
            self._enforcing_topmost = True
            self._last_topmost_apply = time_module.monotonic()
            ctypes.set_last_error(0)
            result = USER32.SetWindowPos(
                wintypes.HWND(int(self.winId())),
                wintypes.HWND(self.HWND_TOPMOST),
                0,
                0,
                0,
                0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
            )
            error = ctypes.get_last_error()
            if not result:
                debug_log(f"SetWindowPos TOPMOST failed: hwnd={int(self.winId())} error={error}")
        except Exception:
            debug_log(f"SetWindowPos TOPMOST failed: {traceback.format_exc()}")
        finally:
            self._enforcing_topmost = False

    def schedule_topmost_reapply(self) -> None:
        """Defer native Z-order repair so Windows message handling cannot recurse."""
        if (
            self._enforcing_topmost
            or self._topmost_reapply_pending
            or not self.config.get("always_on_top", True)
            # SetWindowPos itself sends WM_WINDOWPOSCHANGED.  Ignore that
            # short self-generated burst instead of recursively resetting Z-order.
            or time_module.monotonic() - self._last_topmost_apply < 0.25
        ):
            return
        self._topmost_reapply_pending = True
        def apply() -> None:
            self._topmost_reapply_pending = False
            self.enforce_topmost()

        QTimer.singleShot(80, apply)

    def toggle_visibility(self) -> None:
        if self.isVisible():
            debug_log("Visibility toggle: hide")
            self.hide()
            return
        debug_log("Visibility toggle: show and activate")
        self.showNormal()
        self.enforce_topmost()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.enforce_topmost()

    def _refresh_geometry(self) -> None:
        metrics = QFontMetrics(self._font)
        self._row_height = max(round(self.config["font_size"] * 1.5), metrics.height() + 1)
        rows = [self._row_text(stock) for stock in self.stocks]
        rows.append(self._total_row_text())
        self._column_widths = [max(metrics.horizontalAdvance(row[index]) for row in rows) for index in range(5)]
        text_width = sum(self._column_widths) + self.COLUMN_GAP * 4
        width = text_width + self.HORIZONTAL_PADDING * 2
        height = len(rows) * self._row_height + self.VERTICAL_PADDING * 2
        # Start at exactly one row; additional rows are reached with wheel paging.
        one_row_height = self._row_height + self.VERTICAL_PADDING * 2
        self.setMinimumSize(max(width, 120), one_row_height)
        if not self._geometry_initialized:
            self.resize(max(width, 120), one_row_height)
            self._geometry_initialized = True

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._scroll_row = min(self._scroll_row, self._max_scroll_row())
        debug_log(f"Window resized: x={self.x()} y={self.y()} w={self.width()} h={self.height()}")

    def _move_to_initial_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = self.config["window_pos_x"]
        y = self.config["window_pos_y"]
        if x < 0:
            x = available.right() - self.width() + 1
        if y < 0:
            y = available.top()
        self.move(x, y)

    def _row_text(self, stock: Stock) -> tuple[str, str, str, str, str]:
        quote = self.quotes.get(stock.code, Quote())
        if quote.current_price is None:
            return stock.alias, "--", "--", "--", "--"
        # Display the exact API representation; only calculations use float.
        price_text = quote.current_price_text or str(quote.current_price)
        day_change = percent_change(quote.current_price, quote.previous_close)
        profit_loss = percent_change(quote.current_price, stock.cost_price)
        change_amount = (quote.current_price - (quote.previous_close or 0.0)) * stock.shares
        return stock.alias, price_text, percent_text(day_change), percent_text(profit_loss), f"{change_amount:+.0f}"

    def _total_row_text(self) -> tuple[str, str, str, str, str]:
        """Return the final aggregate row in the same five-column layout.

        The second and fifth values are amounts in hundred-yuan units. Aggregate
        percentages are weighted by total cost and total previous-close value.
        """
        valid: list[tuple[Stock, Quote]] = []
        for stock in self.stocks:
            quote = self.quotes.get(stock.code, Quote())
            if (
                quote.current_price is not None
                and quote.previous_close is not None
                and math.isfinite(quote.current_price)
                and math.isfinite(quote.previous_close)
            ):
                valid.append((stock, quote))
        if not valid:
            return "total", "--", "--", "--", "--"
        profit_amount = sum((quote.current_price - stock.cost_price) * stock.shares for stock, quote in valid)
        day_amount = sum((quote.current_price - quote.previous_close) * stock.shares for stock, quote in valid)
        total_cost = sum(stock.cost_price * stock.shares for stock, _ in valid)
        total_previous = sum(quote.previous_close * stock.shares for stock, quote in valid)
        holding_percent = percent_change(total_cost + profit_amount, total_cost)
        day_percent = percent_change(total_previous + day_amount, total_previous)
        return (
            "total",
            f"{profit_amount / 100:+.0f}",
            percent_text(holding_percent),
            percent_text(day_percent),
            f"{day_amount / 100:+.0f}",
        )

    def fetch_quotes(self, force: bool = False) -> None:
        if self._fetching:
            return
        if not force and not is_query_time(self.config["query_time_ranges"]):
            return
        self._fetching = True
        worker = QuoteFetchWorker(self.stocks, self.config["request_url"], self.config["request_token"])
        worker.signals.completed.connect(self._apply_quotes)
        QThreadPool.globalInstance().start(worker)

    def _apply_quotes(self, quotes: dict[str, Quote]) -> None:
        self._fetching = False
        self.quotes = quotes
        old_size = self.size()
        self._refresh_geometry()
        # Preserve the user's left/top position when data width changes.
        if self.size() != old_size:
            self.move(self.pos())
        self.update()

    def _visible_row_count(self) -> int:
        return max(1, (self.height() - self.VERTICAL_PADDING * 2) // self._row_height)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self._font)
        metrics = QFontMetrics(self._font)
        rows = [*self.stocks, None]
        visible_count = self._visible_row_count()
        end = min(len(rows), self._scroll_row + visible_count)
        for index, stock in enumerate(rows[self._scroll_row:end]):
            y_top = self.VERTICAL_PADDING + index * self._row_height
            baseline = y_top + (self._row_height - metrics.height()) // 2 + metrics.ascent()
            values = self._row_text(stock) if stock is not None else self._total_row_text()
            x = self.HORIZONTAL_PADDING
            for column, value in enumerate(values):
                if column == 0:
                    painter.setPen(self._flat_color)
                else:
                    raw_value = parse_percent(value) if column in (2, 3) else parse_number(value)
                    painter.setPen(self._color_for_value(raw_value))
                painter.drawText(x, baseline, value)
                x += self._column_widths[column] + self.COLUMN_GAP

    def _color_for_value(self, value: float | None) -> QColor:
        if value is None or value == 0:
            return self._flat_color
        return self._up_color if value > 0 else self._down_color

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            local_y = event.position().toPoint().y()
            if local_y <= self.RESIZE_MARGIN:
                self._resize_edge = "top"
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geometry = self.geometry()
                debug_log("Top-edge resize started")
            elif local_y >= self.height() - self.RESIZE_MARGIN:
                self._resize_edge = "bottom"
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geometry = self.geometry()
                debug_log("Bottom-edge resize started")
            else:
                # Use Qt's own mouse capture.  Native caption dragging
                # conflicts with transparent frameless window hit testing.
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                debug_log(f"Drag started at {event.globalPosition().toPoint()}")
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._resize_edge is not None
            and self._resize_start_global is not None
            and self._resize_start_geometry is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            initial = self._resize_start_geometry
            current = event.globalPosition().toPoint()
            minimum_height = self.minimumHeight()
            if self._resize_edge == "top":
                bottom = initial.bottom()
                top = min(current.y(), bottom - minimum_height + 1)
                self.setGeometry(initial.left(), top, initial.width(), bottom - top + 1)
            else:
                height = max(minimum_height, current.y() - initial.top() + 1)
                self.setGeometry(initial.left(), initial.top(), initial.width(), height)
            event.accept()
            return
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        local_y = event.position().toPoint().y()
        if local_y <= self.RESIZE_MARGIN or local_y >= self.height() - self.RESIZE_MARGIN:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._drag_origin = None
        self._resize_edge = None
        self._resize_start_global = None
        self._resize_start_geometry = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._wheel_hook_active:
            # The low-level hook already queued exactly one logical row step.
            event.accept()
            return
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self._move_rows(-1 if delta > 0 else 1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Up:
            self._move_rows(-1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self._move_rows(1)
            event.accept()
            return
        super().keyPressEvent(event)

    def _move_rows(self, amount: int) -> None:
        new_row = max(0, min(self._max_scroll_row(), self._scroll_row + amount))
        debug_log(
            f"Move rows amount={amount} before={self._scroll_row} after={new_row} "
            f"max={self._max_scroll_row()} visible={self._visible_row_count()}"
        )
        if new_row != self._scroll_row:
            self._scroll_row = new_row
            self.update()

    def _max_scroll_row(self) -> int:
        return max(0, len(self.stocks) + 1 - self._visible_row_count())

    def nativeEvent(self, event_type: Any, message: Any) -> tuple[bool, int]:
        """Let Windows own resizing for this frameless top-level window."""
        if event_type in (
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
            "windows_generic_MSG",
            "windows_dispatcher_MSG",
        ):
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == self.WM_WINDOWPOSCHANGED:
                self.schedule_topmost_reapply()
        return super().nativeEvent(event_type, message)


def percent_change(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base == 0 or not math.isfinite(base):
        return None
    value = (current - base) / base * 100
    return value if math.isfinite(value) else None


def percent_text(value: float | None) -> str:
    return "--" if value is None else f"{value:+.2f}%"


def parse_percent(text: str) -> float | None:
    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


def parse_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def main() -> int:
    directory = application_directory()
    cleanup_old_logs(directory, keep_days=3)
    # Startup watermark for diagnostics and build provenance.
    debug_log("author:coutxiao")
    initialize_rid()
    config = load_config(directory)
    stocks = load_stocks(directory)
    app = QApplication(sys.argv)
    # The overlay may be hidden to the notification area, so it must remain alive.
    app.setQuitOnLastWindowClosed(False)
    overlay = StockOverlay(config, stocks)
    interaction_filter = InteractionFilter(overlay)
    app.installEventFilter(interaction_filter)
    overlay.show()
    debug_log(f"Application started hwnd={int(overlay.winId())} size={overlay.width()}x{overlay.height()}")
    wheel_hook = WindowsWheelHook(overlay)
    overlay._wheel_hook_active = wheel_hook.active
    hotkey_filter = WindowsHotkeyFilter(overlay, config)
    app.installNativeEventFilter(hotkey_filter)
    tray_icon = overlay.windowIcon()
    if tray_icon.isNull():
        tray_icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    tray = QSystemTrayIcon(tray_icon, app)
    tray.setToolTip("gp")
    tray_menu = QMenu()
    toggle_action = tray_menu.addAction("显示 / 隐藏")
    toggle_action.triggered.connect(overlay.toggle_visibility)
    tray_menu.addSeparator()
    exit_action = tray_menu.addAction("退出")
    exit_action.triggered.connect(app.quit)
    tray.setContextMenu(tray_menu)

    def restore_on_click(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            overlay.toggle_visibility()

    tray.activated.connect(restore_on_click)
    tray.show()
    result = app.exec()
    wheel_hook.uninstall()
    hotkey_filter.unregister()
    return result


if __name__ == "__main__":
    raise SystemExit(main())

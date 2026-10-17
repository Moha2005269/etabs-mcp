"""Capture the ETABS main window as PNG so an agent can look at the model like an engineer would."""

from __future__ import annotations

import io
import time

from .errors import EtabsMcpError


class ScreenshotError(EtabsMcpError):
    code = "screenshot_failed"


_dpi_set = False


def _ensure_dpi_aware() -> None:
    # Without per-monitor DPI awareness, window rectangles are virtualised and captures come out cropped.
    global _dpi_set
    if _dpi_set:
        return
    _dpi_set = True
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _bring_to_front(hwnd: int) -> None:
    """SetForegroundWindow from a background process is refused unless the caller recently had input; a
    synthetic Alt tap satisfies the foreground lock (the documented workaround)."""
    import ctypes

    import win32con  # type: ignore[import-not-found]
    import win32gui  # type: ignore[import-not-found]

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)  # Alt down
    user32.keybd_event(0x12, 0, 2, 0)  # Alt up
    win32gui.SetForegroundWindow(hwnd)
    win32gui.BringWindowToTop(hwnd)


def capture_window(
    hwnd: int, *, max_width: int = 1600, mode: str = "auto", settle_seconds: float = 0.6
) -> tuple[bytes, dict]:
    """PNG bytes of the ETABS window as it appears on screen, GPU-drawn model views included.
    mode='printwindow' renders off-screen without changing focus, but ETABS's model views come out blank."""
    try:
        import ctypes

        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
        import win32ui  # type: ignore[import-not-found]
        from PIL import Image, ImageGrab
    except ImportError as exc:  # pragma: no cover
        raise ScreenshotError(f"Screenshots need pywin32 and Pillow: {exc}") from exc

    _ensure_dpi_aware()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ScreenshotError("The ETABS window has no visible area (is it hidden?).", hint="Call etabs_show_window.")

    image, method = None, "screen grab"
    if mode in ("auto", "screen"):
        # ETABS draws its model views with the GPU; only the composed screen shows them, so grab the
        # screen with ETABS briefly in front, then give focus back to whatever the user had active.
        previous = win32gui.GetForegroundWindow()
        try:
            _bring_to_front(hwnd)
            time.sleep(settle_seconds)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except Exception:  # noqa: BLE001 - fall back to PrintWindow below
            image = None
        finally:
            if previous and previous != hwnd and win32gui.IsWindow(previous):
                try:
                    _bring_to_front(previous)
                except Exception:  # noqa: BLE001 - focus restore is best effort
                    pass
    if image is None:
        method = "PrintWindow"
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            mem_dc.SelectObject(bitmap)
            # PW_RENDERFULLCONTENT (2); GPU viewports may still come out blank.
            if ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2):
                info = bitmap.GetInfo()
                image = Image.frombuffer(
                    "RGB", (info["bmWidth"], info["bmHeight"]), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1
                )
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            mem_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
    if image is None:
        raise ScreenshotError("Could not capture the ETABS window.")
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), {"width": image.width, "height": image.height, "method": method}

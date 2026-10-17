"""Drive ETABS menu commands through Windows UI Automation.

A few ETABS operations have no API at all (most importantly Design > Shear Wall Design > Start Design/Check).
This module reaches them the way a person does: through the menu bar, using the accessibility tree, which needs
no mouse or keyboard and works while the user keeps typing in other windows.
"""

from __future__ import annotations

import time
from typing import Any

from .errors import EtabsMcpError


class GuiError(EtabsMcpError):
    code = "gui_automation_failed"


def _uia():
    try:
        import comtypes.client  # type: ignore[import-not-found]

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear tool error
        raise GuiError(f"Windows UI Automation is unavailable: {exc}") from exc
    auto = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=U.IUIAutomation)
    return auto, U


def _norm(text: str) -> str:
    return text.replace("&", "").replace("...", "").strip().lower()


def _children(auto, el):
    walker = auto.ControlViewWalker
    c = walker.GetFirstChildElement(el)
    while c:
        yield c
        c = walker.GetNextSiblingElement(c)


def _menu_items(auto, U, scope) -> list:
    cond = auto.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_MenuItemControlTypeId)
    found = scope.FindAll(U.TreeScope_Descendants, cond)
    return [found.GetElement(i) for i in range(found.Length)]


def _expand(U, el) -> None:
    pat = el.GetCurrentPattern(U.UIA_ExpandCollapsePatternId)
    if not pat:
        raise GuiError(f"Menu '{el.CurrentName}' cannot be expanded.")
    pat.QueryInterface(U.IUIAutomationExpandCollapsePattern).Expand()


def _menubar(auto, U, hwnd: int):
    root = auto.ElementFromHandle(hwnd)
    bars = [c for c in _children(auto, root) if c.CurrentControlType == U.UIA_MenuBarControlTypeId]
    if not bars:
        raise GuiError("The ETABS menu bar is not reachable (is a modal dialog open, or the window hidden?).")
    return bars[0]


def _collapse_all(auto, U, hwnd: int) -> None:
    try:
        for item in _children(auto, _menubar(auto, U, hwnd)):
            pat = item.GetCurrentPattern(U.UIA_ExpandCollapsePatternId)
            if pat:
                p = pat.QueryInterface(U.IUIAutomationExpandCollapsePattern)
                if p.CurrentExpandCollapseState != 0:
                    p.Collapse()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def list_menu(hwnd: int, path: list[str] | None = None) -> list[str]:
    """Names of the menu items under `path` (top level when empty)."""
    auto, U = _uia()
    bar = _menubar(auto, U, hwnd)
    if not path:
        return [c.CurrentName for c in _children(auto, bar) if c.CurrentName and not c.CurrentName.startswith("▼")]
    try:
        _open_path(auto, U, hwnd, path, leaf_invoke=False)
        desk = auto.GetRootElement()
        # Items of the deepest open submenu are the newest visible menu items not in the menu bar.
        top = {c.CurrentName for c in _children(auto, bar)}
        names = [m.CurrentName for m in _menu_items(auto, U, desk)]
        return [n for n in names if n and n not in top and n != "System"]
    finally:
        _collapse_all(auto, U, hwnd)


def _open_path(auto, U, hwnd: int, path: list[str], *, leaf_invoke: bool) -> Any:
    bar = _menubar(auto, U, hwnd)
    head = next((c for c in _children(auto, bar) if _norm(c.CurrentName) == _norm(path[0])), None)
    if head is None:
        raise GuiError(f"No top-level menu '{path[0]}'.",
                       hint=f"Menus: {[c.CurrentName for c in _children(auto, bar)]}")
    _expand(U, head)
    time.sleep(0.4)
    current = head
    desk = auto.GetRootElement()
    for depth, name in enumerate(path[1:], start=1):
        cand = None
        for _ in range(10):
            cand = next((m for m in _menu_items(auto, U, desk) if _norm(m.CurrentName) == _norm(name)), None)
            if cand is not None:
                break
            time.sleep(0.2)
        if cand is None:
            visible = [m.CurrentName for m in _menu_items(auto, U, desk) if m.CurrentName][:60]
            _collapse_all(auto, U, hwnd)
            raise GuiError(f"Menu item '{name}' not found under {' > '.join(path[:depth])}.",
                           hint=f"Visible items: {visible}")
        current = cand
        last = depth == len(path) - 1
        if not last:
            _expand(U, current)
            time.sleep(0.4)
    if leaf_invoke:
        pat = current.GetCurrentPattern(U.UIA_InvokePatternId)
        if not pat:
            raise GuiError(f"'{path[-1]}' is a submenu, not a command.")
        pat.QueryInterface(U.IUIAutomationInvokePattern).Invoke()
    return current


def invoke_menu(hwnd: int, path: list[str]) -> None:
    """Invoke a menu command such as ['Design', 'Shear Wall Design', 'Start Design/Check']."""
    auto, U = _uia()
    try:
        _open_path(auto, U, hwnd, path, leaf_invoke=True)
    except GuiError:
        raise
    except Exception as exc:  # noqa: BLE001
        _collapse_all(auto, U, hwnd)
        raise GuiError(f"Could not invoke {' > '.join(path)}: {exc}") from exc


def dialogs(pid: int, main_hwnd: int) -> list[dict[str, Any]]:
    """Visible top-level windows of the ETABS process other than the main window (open dialogs)."""
    import win32gui  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    out: list[dict[str, Any]] = []

    def visit(h: int, _: object) -> bool:
        if h != main_hwnd and win32gui.IsWindowVisible(h):
            _, p = win32process.GetWindowThreadProcessId(h)
            if p == pid:
                title = win32gui.GetWindowText(h)
                if title:
                    out.append({"hwnd": h, "title": title})
        return True

    win32gui.EnumWindows(visit, None)
    return out


def close_dialog(hwnd: int) -> None:
    import win32con  # type: ignore[import-not-found]
    import win32gui  # type: ignore[import-not-found]

    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

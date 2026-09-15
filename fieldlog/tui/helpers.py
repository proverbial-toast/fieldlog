"""Pure helpers for the TUI: shell-token parsing, truncation, clipboard."""

from __future__ import annotations

import base64
import re
import sys
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from textual.app import App


def parse_iface_field(raw: str, current: str) -> Tuple[str, str]:
    """(interface, local address) from the scope form's interface field.

    `tun0 / 10.8.0.2` sets both. A bare `tun0` leaves the address empty, so $LHOST
    follows the interface. A blank name keeps `current`.
    """
    name, _, addr = (raw or "").partition("/")
    return name.strip() or current, addr.strip()


def tokenize(s: str) -> List[str]:
    """Shell-tokenise an argument string, respecting quoted substrings."""
    out: List[str] = []
    cur = ""
    q: Optional[str] = None
    for ch in str(s):
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in ('"', "'"):
            q = ch
            cur += ch
            continue
        if ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def arg_groups(s: str) -> dict:
    """Group flags with their associated parameter values."""
    toks = tokenize(s)
    out: List[dict] = []
    i = 0
    while i < len(toks):
        tk = toks[i]
        if len(tk) > 1 and tk[0] in ("-", "+"):
            vals: List[str] = []
            while i + 1 < len(toks) and not re.match(r"^[-+]\S", toks[i + 1]):
                vals.append(toks[i + 1])
                i += 1
            out.append({"flag": tk, "value": " ".join(vals)})
        else:
            out.append({"flag": "", "value": tk})
        i += 1
    return {"groups": out, "count": len(toks)}


def truncate_right(text: str, width: int) -> str:
    """Explicit right-side truncation. Never bidi — a reordered path is a wrong
    value for something meant to be copied."""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def copy_text_to_clipboard(text: str, app: Optional[App] = None) -> bool:
    """Copy via the Textual clipboard inside the TUI, or stdout OSC 52 outside."""
    copied = False
    if app is not None and hasattr(app, "copy_to_clipboard"):
        try:
            app.copy_to_clipboard(text)
            return True
        except Exception:
            pass
    else:
        try:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sys.stdout.write(f"\033]52;c;{encoded}\007")
            sys.stdout.flush()
            copied = True
        except Exception:
            pass
    try:
        import pyperclip  # type: ignore[import-not-found]
        pyperclip.copy(text)
        copied = True
    except Exception:
        pass
    return copied

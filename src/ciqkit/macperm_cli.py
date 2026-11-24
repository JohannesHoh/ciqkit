"""Ask macOS which privacy permissions this process has. ``python -m ciqkit.macperm_cli``.

Run as a CHILD of the broker, so the answer is for whatever macOS holds
responsible for the broker, and so a crash in a system framework cannot take
the daemon with it. Stdlib only (ctypes).
"""
from __future__ import annotations

import ctypes
import json
import sys

_AS = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


def _accessibility(request: bool) -> bool:
    ax = ctypes.CDLL(_AS)
    if not request:
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    cf = ctypes.CDLL(_CF)
    key = ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt")
    true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    keys, values = (ctypes.c_void_p * 1)(key.value), (ctypes.c_void_p * 1)(true.value)
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    opts = cf.CFDictionaryCreate(
        None, keys, values, 1,
        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
    ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    return bool(ax.AXIsProcessTrustedWithOptions(opts))


def _screen_recording(request: bool) -> bool:
    cg = ctypes.CDLL(_CG)
    fn = cg.CGRequestScreenCaptureAccess if request else cg.CGPreflightScreenCaptureAccess
    fn.restype = ctypes.c_bool
    return bool(fn())


def check(request: bool = False) -> dict:
    out = {}
    for name, probe in (("accessibility", _accessibility),
                        ("screen_recording", _screen_recording)):
        try:
            out[name] = probe(request)
        except (OSError, AttributeError, ValueError):
            out[name] = None   # this macOS cannot say
    return out


if __name__ == "__main__":
    print(json.dumps(check("--request" in sys.argv[1:])))

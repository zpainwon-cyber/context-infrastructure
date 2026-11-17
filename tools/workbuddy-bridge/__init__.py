"""Hermes plugin shell for workbuddy-bridge.

Deliberately inert on load: initialize() only counts registered accounts.
Nothing is written to config.yaml, .env, or the desktop until an operator
runs `python3 wbctl.py init|add|sync`. That keeps installing the plugin
separate from changing production configuration.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wbctl  # noqa: E402


def initialize(*_args, **_kwargs):
    try:
        reg = wbctl.load_registry(wbctl.DEFAULT_HOME)
        return {"ok": True, "accounts": len(reg.get("accounts", []))}
    except Exception:
        return {"ok": True, "accounts": 0}


def main(argv=None):
    return wbctl.main(argv)

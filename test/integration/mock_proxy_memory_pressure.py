#!/usr/bin/env python3
"""Launcher that monkey-patches _get_system_memory to simulate high memory pressure.

Used by test_memory_reject_integration.sh. The proxy will see used_pct=95% and
reject requests with 503.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

# Patch before importing anthropic_proxy
import anthropic_proxy as proxy


def _fake_system_memory():
    return {
        "free_gb": 1.0,
        "wired_gb": 20.0,
        "active_gb": 25.0,
        "inactive_gb": 2.0,
        "compress_gb": 0.0,
        "total_gb": 48.0,
        "used_gb": 45.0,
        "available_gb": 3.0,
        "used_pct": "95.0",
    }


proxy._get_system_memory = _fake_system_memory

if __name__ == "__main__":
    proxy.main()

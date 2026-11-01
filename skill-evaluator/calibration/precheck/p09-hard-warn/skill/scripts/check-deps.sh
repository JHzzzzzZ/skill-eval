#!/usr/bin/env bash
set -u
FAIL=0
bad(){ echo "[FAIL] $1"; FAIL=1; }
warn(){ echo "[warn] $1"; }
command -v python3 >/dev/null 2>&1 || warn "缺 python3"
command -v git >/dev/null 2>&1 || warn "缺 git"
exit 0

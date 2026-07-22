#!/usr/bin/env bash
# ============================================================
#  SaveSync - install dependencies with NO network access (Unix)
#  Installs every requirement from the local offline_deps/
#  folder (populated beforehand with ./download_offline_deps.sh
#  on a machine with the same OS and Python version).
#  Usage:  ./install_offline_deps.sh
# ============================================================
set -u
cd "$(dirname "$0")"

if [ ! -d offline_deps ]; then
    echo "[!!] offline_deps/ folder not found."
    echo "     Run ./download_offline_deps.sh on an online machine first."
    exit 1
fi

PY=""
for cand in python3 python; do
    if command -v "${cand}" >/dev/null 2>&1             && "${cand}" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        PY="${cand}"
        break
    fi
done
if [ -z "${PY}" ]; then
    echo "[!!] No working Python >= 3.10 found in PATH."
    exit 1
fi

"${PY}" -m pip install --no-index --find-links=offline_deps -r requirements.txt
CODE=$?

echo
if [ ${CODE} -eq 0 ]; then
    echo "[OK] All dependencies installed offline. Run:  ${PY} main.py"
else
    echo "[!!] Offline install failed (exit code ${CODE})."
    echo "     Check that offline_deps/ was populated on a machine with"
    echo "     the SAME OS and Python version as this one."
fi
exit ${CODE}

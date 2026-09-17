#!/usr/bin/env bash
# ============================================================
#  SaveSync - populate the offline dependency folder (Unix)
#  Downloads every requirement (wheels for THIS platform and
#  Python version) into offline_deps/ for later offline install.
#  Run once from an ONLINE machine with the same OS/Python as
#  the target machine.   Usage:  ./download_offline_deps.sh
# ============================================================
set -u
cd "$(dirname "$0")"

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

"${PY}" -m pip download -r requirements.txt -d offline_deps
CODE=$?

# langdetect has no wheel on PyPI, only a source dist -- pip download would
# otherwise vendor the .tar.gz, and installing FROM an sdist needs
# setuptools as a build dependency at install time, which is exactly the
# one thing --no-index --find-links can't fall back to PyPI for. Building
# the (universal, pure-Python) wheel here instead sidesteps that: the
# target machine then just installs a wheel, no build step, no setuptools
# needed. Same fix tests/download_offline_deps.sh already uses.
if [ ${CODE} -eq 0 ]; then
    rm -f offline_deps/langdetect-*.tar.gz
    "${PY}" -m pip wheel langdetect --no-deps -w offline_deps
    CODE=$?
fi

echo
if [ ${CODE} -eq 0 ]; then
    echo "[OK] offline_deps/ populated. Copy the whole project folder"
    echo "     to the offline machine and run ./install_offline_deps.sh"
else
    echo "[!!] Download failed (exit code ${CODE})."
fi
exit ${CODE}

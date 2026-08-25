#!/usr/bin/env bash
# ============================================================
#  SaveSync — build a self-contained AppImage (Linux)
#
#  Usage:  ./packaging/build_appimage.sh [--keep-appdir]
#  Output: dist/SaveSync-<version>-<arch>.AppImage
#
#  Needs: python3 >= 3.10 with the project's dependencies, and
#         appimagetool. If appimagetool is not on PATH the script
#         downloads the official release binary into build/ and uses
#         that — nothing is installed system-wide.
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
BUILD="${ROOT}/build/appimage"
APPDIR="${BUILD}/SaveSync.AppDir"
KEEP_APPDIR=0
[ "${1:-}" = "--keep-appdir" ] && KEEP_APPDIR=1

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31m!!\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "AppImages are built on Linux; this is $(uname -s)."

# ── interpreter ─────────────────────────────────────────────────────────
# SAVESYNC_PYTHON names one explicitly. Build machines commonly have
# several — a system python, a pyenv one, the virtualenv that actually
# holds PySide6 — and "whichever comes first on PATH" is the wrong answer
# often enough to be worth an override.
PY="${SAVESYNC_PYTHON:-}"
for cand in python3 python; do
    [ -n "${PY}" ] && break
    if command -v "${cand}" >/dev/null 2>&1 \
       && "${cand}" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="${cand}"; break
    fi
done
[ -n "${PY}" ] || die "no Python >= 3.10 on PATH"

"${PY}" -c 'import PyInstaller' 2>/dev/null \
    || die "PyInstaller is not installed for ${PY} (pip install pyinstaller)"
"${PY}" -c 'import PySide6' 2>/dev/null \
    || die "PySide6 is not installed for ${PY} (pip install -r requirements.txt)"

VERSION="$("${PY}" - <<'PYEOF'
import re, pathlib
src = pathlib.Path("core/constants.py").read_text(encoding="utf-8")
m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)', src)
print(m.group(1) if m else "0.0.0")
PYEOF
)"
ARCH="$(uname -m)"
say "SaveSync ${VERSION} — ${ARCH}"

# ── 1. freeze ───────────────────────────────────────────────────────────
say "PyInstaller (onedir)"
cd "${ROOT}"
"${PY}" -m PyInstaller --clean --noconfirm \
    --distpath "${BUILD}/dist" --workpath "${BUILD}/work" \
    "${HERE}/savesync-linux.spec"

[ -x "${BUILD}/dist/savesync/savesync" ] \
    || die "PyInstaller produced no executable at ${BUILD}/dist/savesync/savesync"

# ── 2. lay out the AppDir ───────────────────────────────────────────────
say "AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin" \
         "${APPDIR}/usr/share/applications" \
         "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

cp -a "${BUILD}/dist/savesync/." "${APPDIR}/usr/bin/"

install -m 755 "${HERE}/AppRun"             "${APPDIR}/AppRun"
install -m 644 "${HERE}/savesync.desktop"   "${APPDIR}/savesync.desktop"
install -m 644 "${HERE}/savesync.desktop"   "${APPDIR}/usr/share/applications/savesync.desktop"
install -m 644 "${ROOT}/assets/icon.png"    "${APPDIR}/savesync.png"
install -m 644 "${ROOT}/assets/icon.png"    "${APPDIR}/usr/share/icons/hicolor/256x256/apps/savesync.png"

# ── fonts ───────────────────────────────────────────────────────────────
# Bundled, and not left to the host. Measured on a clean Ubuntu 24.04: with
# no emoji font installed every icon in the interface (folder, disk, bin,
# refresh) renders as an empty box, and with no CJK font a Japanese title
# or path does the same — which for a library of visual novels is most of
# it. A self-contained build that looks broken on a machine that happens to
# lack two font packages is not self-contained.
#
# Skipped with a word when the build host has not got them, rather than
# failing: the AppImage still works, it just falls back to whatever the
# user's system provides.
say "fonts"
FONT_DIR="${APPDIR}/usr/share/fonts/truetype/savesync"
mkdir -p "${FONT_DIR}"
FOUND_FONTS=0
FONT_SOURCES="
/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf
/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc
"
for font in ${FONT_SOURCES}; do
    if [ -f "${font}" ]; then
        cp -f "${font}" "${FONT_DIR}/" && FOUND_FONTS=$((FOUND_FONTS + 1))
    fi
done
if [ "${FOUND_FONTS}" -eq 0 ]; then
    rmdir "${FONT_DIR}" 2>/dev/null || true
    echo "   no Noto emoji/CJK font on this build host — the AppImage will"
    echo "   use whatever the target system has. Install"
    echo "   fonts-noto-color-emoji and fonts-noto-cjk, then rebuild."
else
    echo "   bundled ${FOUND_FONTS} font file(s)"
fi

# appimagetool insists on both, and gives an unhelpful error when either is
# missing — checked here so the message names the file.
[ -f "${APPDIR}/savesync.desktop" ] || die "AppDir has no .desktop at its root"
[ -f "${APPDIR}/savesync.png" ]     || die "AppDir has no icon at its root"

# ── 3. appimagetool ─────────────────────────────────────────────────────
TOOL="$(command -v appimagetool || true)"
if [ -z "${TOOL}" ]; then
    TOOL="${ROOT}/build/appimagetool-${ARCH}.AppImage"
    if [ ! -x "${TOOL}" ]; then
        say "fetching appimagetool"
        URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
        mkdir -p "${ROOT}/build"
        if command -v curl >/dev/null 2>&1; then
            curl -fL --retry 3 -o "${TOOL}" "${URL}"
        elif command -v wget >/dev/null 2>&1; then
            wget -O "${TOOL}" "${URL}"
        else
            die "neither curl nor wget available, and appimagetool is not on PATH"
        fi
        chmod +x "${TOOL}"
    fi
fi

# FUSE is not available in every container. appimagetool can run without
# it once extracted, which is what --appimage-extract-and-run does.
export APPIMAGE_EXTRACT_AND_RUN=1

say "appimagetool"
mkdir -p "${ROOT}/dist"
OUT="${ROOT}/dist/SaveSync-${VERSION}-${ARCH}.AppImage"
rm -f "${OUT}"
ARCH="${ARCH}" "${TOOL}" "${APPDIR}" "${OUT}"

[ -f "${OUT}" ] || die "appimagetool produced no file"
chmod +x "${OUT}"

if [ "${KEEP_APPDIR}" -eq 0 ]; then
    rm -rf "${BUILD}/dist" "${BUILD}/work"
fi

say "done"
printf '   %s  (%s)\n\n' "${OUT}" "$(du -h "${OUT}" | cut -f1)"
printf 'Run it with:  %s\n' "${OUT}"
printf 'Wayland/X11 is chosen by Qt; force one with SAVESYNC_QT_PLATFORM=xcb\n'

"""Fetch the Tor program that private mode runs (core.p2p.tor) into vendor/tor/<platform>/.

SaveSync carries Tor with it - nothing is installed by the person - so the build needs the
program. This downloads the Tor Project's own "Tor Expert Bundle" (the tor daemon, BSD
licence), checks it against the SHA-256 the project publishes for exactly this release (pinned
below: a different file is refused), and keeps only what is needed: the program, the shared
libraries next to it (Linux), the licences. The pluggable transports (obfs4, snowflake...) and
tor-gencert are left out.

    python tools/fetch_tor.py                    # this PC's platform
    python tools/fetch_tor.py windows-x86_64 linux-x86_64
    python tools/fetch_tor.py windows-x86_64 --archive C:/somewhere/tor-expert-bundle-....tar.gz

The checksums come from https://dist.torproject.org/torbrowser/<VERSION>/sha256sums-signed-build.txt.
That file is signed by the Tor Project's release key; this script does NOT check the signature
(it would need gpg and the key) - to be strict, verify the .asc next to the bundle by hand
before updating the pins when a new release is taken.
"""
import hashlib
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "15.0.25"                    # Tor Browser release the bundle belongs to (tor 0.4.9.14)
BASE = f"https://dist.torproject.org/torbrowser/{VERSION}/"
BUNDLES = {
    "windows-x86_64": ("c1988c701de2f24f7d098d1d6d7f507b58f27740d097f29c246e0524d639b252"),
    "linux-x86_64": ("19cf18726eecb362e0f21d27afa9ea9efe5da928bfdfbb1ceb5fe54519e87f4a"),
    "macos-x86_64": ("e1ae4d4a734d28bb590839188511deb3cebc26fcbce9df66d01b2c452060fe95"),
    "macos-aarch64": ("a3febb6179152b0b4f56a0f5cc57bf5cc8ecef7db35e7c3e9d5a044c8158c26c"),
}
ROOT = Path(__file__).resolve().parents[1]
SKIP = ("tor-gencert",)
PLUGGABLE_TRANSPORTS = ("conjure", "lyrebird", "snowflake", "webtunnel", "obfs")


def this_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"{'windows' if system == 'windows' else 'macos' if system == 'darwin' else 'linux'}-{arch}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(plat: str, archive: Path = None) -> Path:
    if plat not in BUNDLES:
        raise SystemExit(f"no pinned bundle for {plat} (known: {', '.join(BUNDLES)})")
    name = f"tor-expert-bundle-{plat}-{VERSION}.tar.gz"
    tmp = Path(tempfile.mkdtemp(prefix="fetch_tor_"))
    try:
        if archive is None:
            archive = tmp / name
            print(f"downloading {BASE}{name} ...")
            urllib.request.urlretrieve(BASE + name, archive)
        got = sha256(archive)
        if got != BUNDLES[plat]:
            raise SystemExit(f"{archive.name}: SHA-256 {got} is not the pinned {BUNDLES[plat]} - refused")
        print(f"SHA-256 matches the pin ({got[:16]}...)")
        out = ROOT / "vendor" / "tor" / plat
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        licences = []
        with tarfile.open(archive) as tf:
            for m in tf.getmembers():
                parts = Path(m.name).parts
                if not m.isfile() or ".." in parts or Path(m.name).is_absolute():
                    continue
                if len(parts) == 2 and parts[0] == "tor" and not parts[1].startswith(SKIP):
                    dest = out / parts[1]
                elif len(parts) == 2 and parts[0] == "docs" and parts[1].endswith(".txt"):
                    if not parts[1].startswith(PLUGGABLE_TRANSPORTS):       # (their licences go with them, not here)
                        licences.append((parts[1], tf.extractfile(m).read().decode("utf-8", "replace")))
                    continue
                else:
                    continue
                with tf.extractfile(m) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if not dest.name.endswith((".dll", ".txt")):
                    dest.chmod(0o755)
        if not licences:
            raise SystemExit("the bundle holds no licence texts - refused")
        (out / "LICENSE.txt").write_text(
            "The Tor program in this folder is the Tor Project's (https://www.torproject.org/), "
            f"release {VERSION} of the Tor Expert Bundle. Its licences follow.\n\n"
            + "\n\n".join(f"===== {n} =====\n{body}" for n, body in sorted(licences)), encoding="utf-8")
        (out / "VERSION").write_text(f"{VERSION}\n{BUNDLES[plat]}\n", encoding="utf-8")
        exe = out / ("tor.exe" if plat.startswith("windows") else "tor")
        if not exe.is_file():
            raise SystemExit(f"{exe.name} is not in the bundle")
        print(f"-> {out}  ({sum(p.stat().st_size for p in out.iterdir()) / 1e6:.1f} MB)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> None:
    archive = None
    if "--archive" in argv:
        i = argv.index("--archive")
        archive = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    plats = argv or [this_platform()]
    if archive is not None and len(plats) != 1:
        raise SystemExit("--archive goes with exactly one platform")
    for plat in plats:
        fetch(plat, archive)


if __name__ == "__main__":
    main(sys.argv[1:])

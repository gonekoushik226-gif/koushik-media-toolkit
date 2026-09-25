"""Build Koushik Media Toolkit for Windows.

    .venv\\Scripts\\python.exe build.py            # tests, portable EXE, installer
    .venv\\Scripts\\python.exe build.py --help     # options

Steps
  1. Run the automated tests (skip with --skip-tests).
  2. Collect ffmpeg.exe / ffprobe.exe into vendor/ffmpeg (from --ffmpeg-dir,
     the KMT_FFMPEG_DIR environment variable, or the PATH).
  3. PyInstaller "one folder" build  -> dist/KoushikMediaToolkit/  (used by the installer)
  4. PyInstaller "one file" build    -> dist/KoushikMediaToolkit-Portable.exe
  5. Run the packaged self-test (--self-test) on both builds.
  6. Compile the Inno Setup installer (needs Inno Setup 6: winget install JRSoftware.InnoSetup).
  7. Copy the results to release/ and write SHA256 checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import APP_ID, APP_NAME, APP_PUBLISHER, __version__  # noqa: E402

DIST = ROOT / "dist"
BUILD = ROOT / "build"
RELEASE = ROOT / "release"
VENDOR = ROOT / "vendor" / "ffmpeg"
ISS = ROOT / "installer" / "KoushikMediaToolkit.iss"

# Modules that PyInstaller might pull in but the app never uses.
EXCLUDES = [
    "tkinter", "_tkinter", "unittest", "pydoc", "doctest", "lib2to3", "test", "numpy", "pandas", "matplotlib",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtPdf",
    "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.QtDBus", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtXml", "PySide6.QtUiTools", "PySide6.QtConcurrent",
]


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def run(cmd: list[str], **kwargs) -> None:
    print(">", subprocess.list2cmdline([str(c) for c in cmd]), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def ensure_ffmpeg(ffmpeg_dir: str | None) -> None:
    """Copy ffmpeg.exe / ffprobe.exe (+ license) into vendor/ffmpeg."""
    VENDOR.mkdir(parents=True, exist_ok=True)
    if (VENDOR / "ffmpeg.exe").is_file() and (VENDOR / "ffprobe.exe").is_file() and not ffmpeg_dir:
        print(f"Using FFmpeg already in {VENDOR}")
        return
    source = ffmpeg_dir or os.environ.get("KMT_FFMPEG_DIR")
    if source:
        folder = Path(source)
    else:
        found = shutil.which("ffmpeg")
        if not found:
            sys.exit("FFmpeg not found. Pass --ffmpeg-dir C:\\path\\to\\ffmpeg\\bin or put ffmpeg on the PATH.")
        folder = Path(found).resolve().parent
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        src = folder / name
        if not src.is_file():
            sys.exit(f"{src} does not exist.")
        shutil.copy2(src, VENDOR / name)
        print(f"Copied {src} -> {VENDOR / name}")
    for candidate in (folder / "LICENSE.txt", folder.parent / "LICENSE.txt", folder / "LICENSE", folder.parent / "LICENSE"):
        if candidate.is_file():
            shutil.copy2(candidate, VENDOR / "FFmpeg-LICENSE.txt")
            break
    else:
        (VENDOR / "FFmpeg-LICENSE.txt").write_text(
            "FFmpeg is licensed under the GNU LGPL/GPL. Source code: https://ffmpeg.org/download.html\n", encoding="utf-8")


def write_version_file() -> Path:
    parts = [int(p) for p in __version__.split(".")] + [0] * (4 - len(__version__.split(".")))
    version_tuple = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_tuple}, prodvers={version_tuple}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{APP_PUBLISHER}'),
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_ID}'),
      StringStruct('LegalCopyright', 'Copyright (C) {time.strftime("%Y")} {APP_PUBLISHER}'),
      StringStruct('OriginalFilename', '{APP_ID}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def pyinstaller(onefile: bool, version_file: Path) -> Path:
    name = f"{APP_ID}-Portable" if onefile else APP_ID
    sep = os.pathsep
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", name,
        "--icon", ROOT / "assets" / "app.ico",
        "--version-file", version_file,
        "--distpath", DIST,
        "--workpath", BUILD / ("pyinstaller-onefile" if onefile else "pyinstaller-onedir"),
        "--specpath", BUILD,
        "--add-data", f"{ROOT / 'assets'}{sep}assets",
        # Data (not binary) so PyInstaller does not try to analyse the 100+ MB executables.
        "--add-data", f"{VENDOR / 'ffmpeg.exe'}{sep}tools",
        "--add-data", f"{VENDOR / 'ffprobe.exe'}{sep}tools",
        "--add-data", f"{VENDOR / 'FFmpeg-LICENSE.txt'}{sep}tools",
        "--collect-data", "yt_dlp_ejs",
        "--collect-data", "certifi",
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append("--onefile" if onefile else "--onedir")
    args.append(ROOT / "run.py")
    run(args, cwd=ROOT)
    return DIST / f"{name}.exe" if onefile else DIST / name / f"{APP_ID}.exe"


def self_test(exe: Path) -> dict:
    report_file = BUILD / f"selftest-{exe.stem}.json"
    report_file.unlink(missing_ok=True)
    started = time.monotonic()
    result = subprocess.run([str(exe), "--self-test", "--self-test-output", str(report_file)], timeout=600)
    elapsed = time.monotonic() - started
    if not report_file.exists():
        sys.exit(f"Self-test of {exe.name} wrote no report (exit code {result.returncode}).")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    for name, check in report["checks"].items():
        status = "ok" if check["ok"] else "FAILED"
        print(f"  {name:<16} {status}  {check.get('info') if check['ok'] else check.get('error')}"[:220])
    print(f"  total {elapsed:.1f}s, exit code {result.returncode}")
    if result.returncode != 0 or not report.get("passed"):
        sys.exit(f"Self-test of {exe.name} FAILED - see {report_file}")
    return report


def find_iscc() -> Path | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
    ]
    found = shutil.which("iscc")
    if found:
        candidates.insert(0, Path(found))
    return next((c for c in candidates if c.is_file()), None)


def build_installer(onedir: Path) -> Path:
    iscc = find_iscc()
    if iscc is None:
        sys.exit("Inno Setup 6 was not found. Install it with:  winget install JRSoftware.InnoSetup")
    RELEASE.mkdir(exist_ok=True)
    run([iscc, f"/DAppVersion={__version__}", f"/DSourceDir={onedir}", f"/DOutputDir={RELEASE}", ISS], cwd=ISS.parent)
    return RELEASE / f"{APP_ID}-{__version__}-Setup.exe"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-tests", action="store_true", help="do not run the test suite first")
    parser.add_argument("--skip-portable", action="store_true", help="do not build the one-file portable EXE")
    parser.add_argument("--skip-installer", action="store_true", help="do not compile the installer")
    parser.add_argument("--ffmpeg-dir", help="folder containing ffmpeg.exe and ffprobe.exe to bundle")
    args = parser.parse_args()
    started = time.monotonic()

    if not args.skip_tests:
        step("Running tests")
        run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT)

    step("Preparing FFmpeg")
    ensure_ffmpeg(args.ffmpeg_dir)
    version_file = write_version_file()

    step("Building the application folder (PyInstaller --onedir)")
    onedir_exe = pyinstaller(onefile=False, version_file=version_file)
    step("Self-test: application folder")
    self_test(onedir_exe)

    artifacts: list[Path] = []
    RELEASE.mkdir(exist_ok=True)
    if not args.skip_portable:
        step("Building the portable EXE (PyInstaller --onefile)")
        portable = pyinstaller(onefile=True, version_file=version_file)
        step("Self-test: portable EXE")
        self_test(portable)
        target = RELEASE / f"{APP_ID}-{__version__}-Portable.exe"
        shutil.copy2(portable, target)
        artifacts.append(target)

    if not args.skip_installer:
        step("Compiling the installer (Inno Setup)")
        artifacts.append(build_installer(onedir_exe.parent))

    step("Release files")
    lines = []
    for path in artifacts:
        lines.append(f"{sha256(path)}  {path.name}")
        print(f"  {path}  ({path.stat().st_size / 1024 ** 2:.1f} MB)")
    if lines:
        (RELEASE / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nDone in {(time.monotonic() - started) / 60:.1f} minutes.")


if __name__ == "__main__":
    main()

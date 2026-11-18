"""Build a standalone BuzzDrop for the current OS with PyInstaller.

    pip install pyinstaller
    python packaging/build.py [--asset-name NAME]

Produces in ./dist:
    Windows  BuzzDrop.exe            (single file, ~10 MB)
    macOS    BuzzDrop.app + .zip     (BZ:// links + drop-on-Dock registered in Info.plist)
    Linux    BuzzDrop                (single file)
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from buzzdrop import APP_NAME, __version__  # noqa: E402

DIST, WORK = ROOT / "dist", ROOT / "build"
ASSETS = ROOT / "buzzdrop" / "assets"

# Stdlib parts the app never imports at runtime — keeps the bundle small.
EXCLUDES = [
    "unittest", "pydoc", "doctest", "pdb", "lib2to3", "sqlite3", "xmlrpc", "xml", "multiprocessing",
    "asyncio", "concurrent", "distutils", "setuptools", "pip", "idlelib", "turtle", "turtledemo",
    "tkinter.test", "test", "curses", "dbm", "pydoc_data", "ensurepip", "venv", "wsgiref", "mailbox",
    "imaplib", "smtplib", "poplib", "nntplib", "ftplib", "telnetlib", "cgi", "cgitb", "difflib",
]

VERSION_FILE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}, 0), prodvers=({v}, 0), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'BuzzDrop'),
      StringStruct('FileDescription', 'BuzzDrop — send & receive files with BuzzHeavier'),
      StringStruct('FileVersion', '{s}'), StringStruct('InternalName', 'BuzzDrop'),
      StringStruct('LegalCopyright', 'MIT License'), StringStruct('OriginalFilename', 'BuzzDrop.exe'),
      StringStruct('ProductName', 'BuzzDrop'), StringStruct('ProductVersion', '{s}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def run(cmd: list) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def patch_mac_bundle(app: Path) -> None:
    plist_path = app / "Contents" / "Info.plist"
    with open(plist_path, "rb") as fh:
        info = plistlib.load(fh)
    info.update({
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "CFBundleURLTypes": [{"CFBundleURLName": "BuzzHeavier link", "CFBundleURLSchemes": ["bz", "BZ"]}],
        "CFBundleDocumentTypes": [{"CFBundleTypeName": "Any file", "CFBundleTypeRole": "Viewer",
                                   "LSHandlerRank": "Alternate", "LSItemContentTypes": ["public.item"]}],
    })
    with open(plist_path, "wb") as fh:
        plistlib.dump(info, fh)
    run(["codesign", "--force", "--deep", "--sign", "-", app])  # re-sign (ad-hoc) after editing


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-name", help="rename the final artifact (used by CI releases)")
    args = ap.parse_args()

    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    is_win, is_mac = sys.platform == "win32", sys.platform == "darwin"

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
           "--name", APP_NAME, "--windowed",
           "--distpath", DIST, "--workpath", WORK / "work", "--specpath", WORK,
           "--add-data", "%s%sbuzzdrop/assets" % (ASSETS, os.pathsep)]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    if is_win:
        vf = WORK / "version.txt"
        nums = ", ".join((__version__.split(".") + ["0", "0"])[:3])
        vf.write_text(VERSION_FILE.format(v=nums, s=__version__), encoding="utf-8")
        cmd += ["--onefile", "--icon", ASSETS / "icon.ico", "--version-file", vf]
    elif is_mac:
        cmd += ["--onedir", "--icon", ASSETS / "icon.icns", "--osx-bundle-identifier", "app.buzzdrop"]
    else:
        cmd += ["--onefile"]
    cmd.append(ROOT / "BuzzDrop.pyw")
    run(cmd)

    if is_mac:
        app = DIST / (APP_NAME + ".app")
        patch_mac_bundle(app)
        shutil.rmtree(DIST / APP_NAME, ignore_errors=True)  # the loose onedir copy
        out = DIST / (args.asset_name or "%s-macos.zip" % APP_NAME)
        run(["ditto", "-c", "-k", "--keepParent", app, out])
    else:
        out = DIST / (APP_NAME + (".exe" if is_win else ""))
        if args.asset_name:
            out = out.rename(DIST / args.asset_name)
    size = out.stat().st_size / 1024 / 1024
    print("\nBuilt %s (%.1f MB)" % (out, size))


if __name__ == "__main__":
    main()

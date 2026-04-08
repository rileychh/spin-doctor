import subprocess
import tomllib
from pathlib import Path
from setuptools import setup

# py2app rejects install_requires (auto-populated from pyproject.toml dependencies).
# Patch py2app's check to skip this validation.
import py2app.build_app
_orig_finalize = py2app.build_app.py2app.finalize_options
def _patched_finalize(self):
    self.distribution.install_requires = None
    _orig_finalize(self)
py2app.build_app.py2app.finalize_options = _patched_finalize

ROOT = Path(__file__).parent

with open(ROOT / "pyproject.toml", "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

# Compile the Icon Composer source bundle into an .icns (legacy fallback) and
# an Assets.car (layered icon used on macOS 26+). Outputs land in build/icon/
# so they're regenerated on every build and never committed.
ICON_SOURCE = ROOT / "resources" / "SpinDoctor.icon"
ICON_BUILD_DIR = ROOT / "build" / "icon"
ICON_BUILD_DIR.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        "xcrun", "actool", str(ICON_SOURCE),
        "--app-icon", "SpinDoctor",
        "--compile", str(ICON_BUILD_DIR),
        "--platform", "macosx",
        "--minimum-deployment-target", "13.0",
        "--output-partial-info-plist", str(ICON_BUILD_DIR / "partial.plist"),
        "--output-format", "human-readable-text",
        "--errors", "--warnings",
    ],
    check=True,
)

APP = ["launch.py"]
DATA_FILES = [
    ("", [
        "src/spin_doctor/resources/menu-bar-extras.png",
        "src/spin_doctor/resources/menu-bar-extras@2x.png",
        str(ICON_BUILD_DIR / "Assets.car"),
    ]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": str(ICON_BUILD_DIR / "SpinDoctor.icns"),
    "plist": {
        "CFBundleName": "Spin Doctor",
        "CFBundleIdentifier": "com.spindoctor.app",
        "CFBundleShortVersionString": VERSION,
        "NSHumanReadableCopyright": "Copyright © 2026 Riley Ho.",
        "LSUIElement": True,
        # Enables the layered Icon Composer icon from Assets.car on macOS 26+.
        # Older macOS falls back to CFBundleIconFile (set by py2app from iconfile).
        "CFBundleIconName": "SpinDoctor",
    },
    "packages": ["spin_doctor", "rumps", "psutil"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)

from setuptools import setup

# py2app rejects install_requires (auto-populated from pyproject.toml dependencies).
# Patch py2app's check to skip this validation.
import py2app.build_app
_orig_finalize = py2app.build_app.py2app.finalize_options
def _patched_finalize(self):
    self.distribution.install_requires = None
    _orig_finalize(self)
py2app.build_app.py2app.finalize_options = _patched_finalize

APP = ["launch.py"]
DATA_FILES = [
    ("", [
        "src/spindoctor/resources/menu-bar-extras.png",
        "src/spindoctor/resources/menu-bar-extras@2x.png",
    ]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "src/spindoctor/resources/SpinDoctor.icns",
    "plist": {
        "CFBundleName": "SpinDoctor",
        "CFBundleIdentifier": "com.spindoctor.app",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
    },
    "packages": ["spindoctor", "rumps", "psutil"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)

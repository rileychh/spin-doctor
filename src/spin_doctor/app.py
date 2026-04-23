import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tomllib

import objc
import psutil
import rumps

BUNDLE_ID = "com.spindoctor.app"
RESOURCES_DIR = Path(__file__).parent / "resources"
CONFIG_DIR = Path.home() / ".config" / "spin_doctor"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_PATH = CONFIG_DIR / "state.json"
LOG_DIR = Path.home() / "Library" / "Logs" / "Spin Doctor"
LOG_PATH = LOG_DIR / "spin-doctor.log"

log = logging.getLogger("spin_doctor")


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))

DEFAULT_CONFIG = {
    "cpu_threshold": 95.0,
    "duration_seconds": 60,
    "check_interval": 5,
    "cooldown_seconds": 300,
}

BUILTIN_IGNORED_PROCESSES = [
    "kernel_task",
    "WindowServer",
    "coreaudiod",
    "loginwindow",
    "launchd",
    "duetexpertd",
    "contactsd",
    "spotlightknowledged",
    "spotlightknowledged.updater",
    "corespotlightd",
    "routined",
    "mediaanalysisd",
    "fileproviderd",
    "FileProviderExt",
    "FPCKService",
    "voicememod",
    "suggestd",
    "IntelligencePlatformComputeService",
    "appstoreagent",
    "triald",
    "UsageTrackingAgent",
    "XProtectRemediatorPirrit",
    "XProtectRemediatorMRTv3",
    "XProtectRemediatorSheepSwap",
    "XProtectRemediatorAdload",
]

BUILTIN_IGNORED_SUFFIXES = [
    "(Renderer)",
]

DEFAULT_CONFIG_TOML = """\
cpu_threshold = 95.0  # % on a single core
duration_seconds = 60  # sustained time before alerting
check_interval = 5  # seconds between polls
cooldown_seconds = 300  # don't re-notify same process name within this window

# The app ships with a built-in skip list of system daemons and
# Electron renderer processes.  Use the keys below to customise it
# without replacing the defaults:
#
# extra_ignored_processes = ["some_process"]
# extra_ignored_suffixes = ["(GPU)"]
# unignore_processes = ["contactsd"]  # re-enable a built-in entry
"""

# --- Config & state ---


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)

    user_config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user_config = tomllib.load(f)

    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in user_config.items() if k in DEFAULT_CONFIG})

    unignore = set(user_config.get("unignore_processes", []))
    config["ignored_processes"] = (
        [p for p in BUILTIN_IGNORED_PROCESSES if p not in unignore]
        + user_config.get("extra_ignored_processes", [])
    )
    config["ignored_suffixes"] = (
        list(BUILTIN_IGNORED_SUFFIXES)
        + user_config.get("extra_ignored_suffixes", [])
    )
    return config


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


# --- Plist & bundle helpers ---


def ensure_plist():
    """Create Info.plist next to the Python binary so rumps notifications work."""
    plist_path = Path(sys.executable).parent / "Info.plist"
    if not plist_path.exists():
        subprocess.run(
            [
                "/usr/libexec/PlistBuddy",
                "-c",
                'Add :CFBundleIdentifier string "com.spindoctor.app"',
                str(plist_path),
            ],
            check=True,
        )


def is_app_bundle() -> bool:
    exe = Path(sys.executable)
    return any(p.suffix == ".app" for p in exe.parents)


# --- Login item (ServiceManagement) ---

_sm_bundle = {}
objc.loadBundle(  # type: ignore[attr-defined]
    "ServiceManagement",
    _sm_bundle,
    bundle_path="/System/Library/Frameworks/ServiceManagement.framework",
)
SMAppService = _sm_bundle["SMAppService"]


def _login_service():
    return SMAppService.alloc().initWithType_identifier_(0, BUNDLE_ID)


def login_item_enabled() -> bool:
    return _login_service().status() == 1  # SMAppServiceStatusEnabled


def set_login_item(enabled: bool):
    service = _login_service()
    if enabled:
        service.registerAndReturnError_(None)
    else:
        service.unregisterAndReturnError_(None)


@dataclass
class TrackedProcess:
    pid: int
    name: str
    first_seen: float
    last_seen: float
    notified: bool = False


class SpinDoctorApp(rumps.App):
    def __init__(self):
        icon_path = str(RESOURCES_DIR / "menu-bar-extras@2x.png")
        super().__init__("Spin Doctor", icon=icon_path, template=True, quit_button=None)  # type: ignore[arg-type]
        self._icon_nsimage.setSize_((18, 18))  # type: ignore[union-attr]
        self.config = load_config()
        self._config_mtime = self._get_config_mtime()
        log.info(
            "Config loaded: threshold=%s%% duration=%ss interval=%ss cooldown=%ss ignored=%d suffixes=%s",
            self.config["cpu_threshold"],
            self.config["duration_seconds"],
            self.config["check_interval"],
            self.config["cooldown_seconds"],
            len(self.config["ignored_processes"]),
            self.config["ignored_suffixes"],
        )
        self.tracked: dict[int, TrackedProcess] = {}
        self.cooldowns: dict[str, float] = {}
        self.my_pid = os.getpid()
        self.kill_menu_items: list[rumps.MenuItem] = []

        self.in_bundle = is_app_bundle()
        self.login_item = rumps.MenuItem("Launch on Login")
        if self.in_bundle:
            state = load_state()
            if "launch_on_login_initialized" not in state:
                set_login_item(True)
                state["launch_on_login_initialized"] = True
                save_state(state)
            self.login_item.state = login_item_enabled()
            self.login_item.set_callback(self.toggle_login)
        else:
            self.login_item.state = False
            self.login_item.set_callback(None)

        self.status_item = rumps.MenuItem("No busy processes detected")
        self.status_item.set_callback(None)
        self.menu = [
            self.status_item,
            None,
            self.login_item,
            rumps.MenuItem("Reload Config", callback=self.reload_config),
            rumps.MenuItem("Open Config File", callback=self.open_config),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        self.timer = rumps.Timer(self.poll, self.config["check_interval"])
        self.timer.start()

    @rumps.notifications
    def on_notification(self, notification):
        if (
            notification.activation_type == "action_button_clicked"
            and notification.data
        ):
            pid = notification.data.get("pid")
            name = notification.data.get("name")
            if pid is not None and name is not None:
                self.kill_process(pid, name)

    def toggle_login(self, sender):
        new_state = not sender.state
        set_login_item(new_state)
        sender.state = login_item_enabled()

    @staticmethod
    def _get_config_mtime() -> float:
        try:
            return CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _apply_config(self):
        self.config = load_config()
        self._config_mtime = self._get_config_mtime()
        self.timer.stop()
        self.timer = rumps.Timer(self.poll, self.config["check_interval"])
        self.timer.start()
        log.info(
            "Config loaded: threshold=%s%% duration=%ss interval=%ss cooldown=%ss ignored=%d suffixes=%s",
            self.config["cpu_threshold"],
            self.config["duration_seconds"],
            self.config["check_interval"],
            self.config["cooldown_seconds"],
            len(self.config["ignored_processes"]),
            self.config["ignored_suffixes"],
        )

    def reload_config(self, _):
        self._apply_config()
        rumps.notification("Spin Doctor", "", "Config reloaded.")

    def open_config(self, _):
        if not CONFIG_PATH.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
        subprocess.run(["open", str(CONFIG_PATH)])

    def poll(self, _):
        mtime = self._get_config_mtime()
        if mtime != self._config_mtime:
            self._apply_config()

        now = time.time()
        threshold = self.config["cpu_threshold"]
        ignored = set(self.config["ignored_processes"])
        ignored_suffixes = tuple(self.config.get("ignored_suffixes", []))

        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                pid = proc.info["pid"]
                name = proc.info["name"]
                cpu = proc.info["cpu_percent"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

            if pid == self.my_pid or cpu is None:
                continue
            if name in ignored or name.endswith(ignored_suffixes):
                continue

            if cpu >= threshold:
                if pid in self.tracked:
                    self.tracked[pid].last_seen = now
                else:
                    self.tracked[pid] = TrackedProcess(
                        pid=pid, name=name, first_seen=now, last_seen=now
                    )
                    log.info(
                        "Tracking %s (PID %d) at %.1f%% CPU",
                        name,
                        pid,
                        cpu,
                    )

        stale = [
            pid
            for pid, tp in self.tracked.items()
            if now - tp.last_seen > self.config["check_interval"] * 2
        ]
        for pid in stale:
            del self.tracked[pid]

        busy_processes: list[TrackedProcess] = []
        for tp in list(self.tracked.values()):
            elapsed = now - tp.first_seen
            if elapsed >= self.config["duration_seconds"]:
                busy_processes.append(tp)
                if not tp.notified:
                    cooldown_key = tp.name
                    last_cooldown = self.cooldowns.get(cooldown_key, 0)
                    if now - last_cooldown >= self.config["cooldown_seconds"]:
                        self.send_notification(tp)
                        tp.notified = True
                        self.cooldowns[cooldown_key] = now

        self.update_menu(busy_processes)

    def update_menu(self, busy_processes: list[TrackedProcess]):
        for item in self.kill_menu_items:
            item._menuitem.menu().removeItem_(item._menuitem)
        self.kill_menu_items.clear()

        if busy_processes:
            count = len(busy_processes)
            label = "process" if count == 1 else "processes"
            self.status_item.title = f"{count} busy {label} detected"

            ns_menu = self.status_item._menuitem.menu()
            status_index = ns_menu.indexOfItem_(self.status_item._menuitem)

            for i, tp in enumerate(busy_processes):
                elapsed = int(time.time() - tp.first_seen)
                title = f"Kill {tp.name} (PID {tp.pid}, {elapsed}s)"
                item = rumps.MenuItem(title)
                item.set_callback(self.make_kill_callback(tp.pid, tp.name))
                ns_menu.insertItem_atIndex_(item._menuitem, status_index + 1 + i)
                self.kill_menu_items.append(item)
        else:
            self.status_item.title = "No busy processes detected"

    def make_kill_callback(self, pid: int, name: str):
        def callback(_):
            self.kill_process(pid, name)

        return callback

    def send_notification(self, tp: TrackedProcess):
        duration = int(time.time() - tp.first_seen)
        log.warning(
            "Alert: %s (PID %d) sustained ≥%s%% CPU for %ds",
            tp.name,
            tp.pid,
            self.config["cpu_threshold"],
            duration,
        )
        rumps.notification(
            "Busy Process Detected",
            f"{tp.name} (PID {tp.pid})",
            f"Using ≥{self.config['cpu_threshold']}% CPU for {duration}s",
            data={"pid": tp.pid, "name": tp.name},
            action_button="Kill",
        )

    def kill_process(self, pid: int, expected_name: str):
        log.info("Kill requested: %s (PID %d)", expected_name, pid)
        try:
            proc = psutil.Process(pid)
            if proc.name() != expected_name:
                log.warning(
                    "Kill aborted: PID %d is now '%s', not '%s' (recycled PID)",
                    pid,
                    proc.name(),
                    expected_name,
                )
                rumps.notification(
                    "Spin Doctor",
                    "Kill aborted",
                    f"PID {pid} is now '{proc.name()}', not '{expected_name}' (recycled PID).",
                )
                return
            proc.terminate()
            try:
                proc.wait(timeout=3)
                log.info("Terminated %s (PID %d) with SIGTERM", expected_name, pid)
            except psutil.TimeoutExpired:
                proc.kill()
                log.info("Killed %s (PID %d) with SIGKILL after timeout", expected_name, pid)
            rumps.notification(
                "Spin Doctor", "", f"Killed {expected_name} (PID {pid})."
            )
            self.tracked.pop(pid, None)
        except psutil.NoSuchProcess:
            log.info("%s (PID %d) already exited", expected_name, pid)
            rumps.notification(
                "Spin Doctor", "", f"{expected_name} (PID {pid}) already exited."
            )
            self.tracked.pop(pid, None)
        except psutil.AccessDenied:
            log.error("Kill failed: access denied for %s (PID %d)", expected_name, pid)
            rumps.notification(
                "Spin Doctor",
                "Kill failed",
                f"Access denied for {expected_name} (PID {pid}).",
            )


def main():
    ensure_plist()
    setup_logging()
    log.info("Spin Doctor starting (bundle=%s)", is_app_bundle())
    SpinDoctorApp().run()


if __name__ == "__main__":
    main()

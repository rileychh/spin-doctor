import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
import tomllib

import psutil
import rumps

RESOURCES_DIR = Path(__file__).parent / "resources"
CONFIG_DIR = Path.home() / ".config" / "spin_doctor"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG = {
    "cpu_threshold": 95.0,
    "duration_seconds": 60,
    "check_interval": 5,
    "cooldown_seconds": 300,
    "ignored_processes": [
        "kernel_task",
        "WindowServer",
        "coreaudiod",
        "loginwindow",
        "launchd",
    ],
}

DEFAULT_CONFIG_TOML = """\
cpu_threshold = 95.0  # % on a single core
duration_seconds = 60  # sustained time before alerting
check_interval = 5  # seconds between polls
cooldown_seconds = 300  # don't re-notify same process name within this window

ignored_processes = [
  "kernel_task",
  "WindowServer",
  "coreaudiod",
  "loginwindow",
  "launchd",
]
"""


def ensure_plist():
    """Create Info.plist next to the Python binary so rumps notifications work."""
    plist_path = Path(os.sys.executable).parent / "Info.plist"
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


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
        return dict(DEFAULT_CONFIG)

    with open(CONFIG_PATH, "rb") as f:
        user_config = tomllib.load(f)

    config = dict(DEFAULT_CONFIG)
    config.update(user_config)
    return config


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
        super().__init__("Spin Doctor", icon=icon_path, template=True, quit_button=None)
        self._icon_nsimage.setSize_((18, 18))
        self.config = load_config()
        self.tracked: dict[int, TrackedProcess] = {}
        self.cooldowns: dict[str, float] = {}
        self.my_pid = os.getpid()
        self.kill_menu_items: list[rumps.MenuItem] = []

        self.status_item = rumps.MenuItem("No busy processes detected")
        self.status_item.set_callback(None)
        self.menu = [
            self.status_item,
            None,
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

    def reload_config(self, _):
        self.config = load_config()
        self.timer.stop()
        self.timer = rumps.Timer(self.poll, self.config["check_interval"])
        self.timer.start()
        rumps.notification("Spin Doctor", "", "Config reloaded.")

    def open_config(self, _):
        if not CONFIG_PATH.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
        os.system(f"open '{CONFIG_PATH}'")

    def poll(self, _):
        now = time.time()
        threshold = self.config["cpu_threshold"]
        ignored = set(self.config["ignored_processes"])

        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                pid = proc.info["pid"]
                name = proc.info["name"]
                cpu = proc.info["cpu_percent"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

            if pid == self.my_pid or name in ignored or cpu is None:
                continue

            if cpu >= threshold:
                if pid in self.tracked:
                    self.tracked[pid].last_seen = now
                else:
                    self.tracked[pid] = TrackedProcess(
                        pid=pid, name=name, first_seen=now, last_seen=now
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
        print(
            f"[Spin Doctor] Alert: {tp.name} (PID {tp.pid}) busy for {duration}s",
            flush=True,
        )
        rumps.notification(
            "Busy Process Detected",
            f"{tp.name} (PID {tp.pid})",
            f"Using ≥{self.config['cpu_threshold']}% CPU for {duration}s",
            data={"pid": tp.pid, "name": tp.name},
            action_button="Kill",
        )

    def kill_process(self, pid: int, expected_name: str):
        try:
            proc = psutil.Process(pid)
            if proc.name() != expected_name:
                rumps.notification(
                    "Spin Doctor",
                    "Kill aborted",
                    f"PID {pid} is now '{proc.name()}', not '{expected_name}' (recycled PID).",
                )
                return
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            rumps.notification("Spin Doctor", "", f"Killed {expected_name} (PID {pid}).")
            self.tracked.pop(pid, None)
        except psutil.NoSuchProcess:
            rumps.notification(
                "Spin Doctor", "", f"{expected_name} (PID {pid}) already exited."
            )
            self.tracked.pop(pid, None)
        except psutil.AccessDenied:
            rumps.notification(
                "Spin Doctor",
                "Kill failed",
                f"Access denied for {expected_name} (PID {pid}).",
            )


def main():
    ensure_plist()
    SpinDoctorApp().run()


if __name__ == "__main__":
    main()

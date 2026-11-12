#!/usr/bin/env python3
"""Inspect or remove a narrowly identified Tencent iOA installation on macOS.

Default: inspection only. No third-party Python dependencies. See README.md.
"""

import argparse
import ctypes
from dataclasses import asdict, dataclass
import datetime
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.0"
APP_ROOTS = (Path("/Applications/QQPCMgr.localized"), Path("/Library/QQPCMgr.localized"))
HELPER = Path("/Library/PrivilegedHelperTools/PreLoginQQPCMgr.app")
TEAM_ID = "PGGDZWR6S3"
RECEIPT = "com.tencent.iOA"
LABELS = {
    "com.tencent.runmgr", "com.tencent.QQPCGuardMonitor", "com.tencent.QQPCMgr",
    "com.tencent.iOAWatermark.Daemon", "com.tencent.PreLoginQQPCMgr.Daemon",
    "com.tencent.iOAPolicyMgr.Daemon",
}
SUPERVISORS = {
    "runmgr", "ztsmwechat", "QQPCGuardMonitor", "QQPCRollBackDeamon", "QQPCMgrDaemon", "NGNMgr",
}
PRODUCT_MARKERS = ("com.tencent.mac.ioa", "com.tencent.ioa", "qqpc", "smartvpn")
# Exact names only: never delete all of com.tencent.*, Tencent, QQ or WeChat.
DATA_NAMES = (
    "com.tencent.QQPCMgr", "Tencent.Uninstaller", "com.tencent.runmgr",
    "com.tencent.QQPCGuardMonitor", "com.tencent.QQPCPolicyMgr.dlp",
    "com.tencent.mac.iOAPolicyMgr", "QQPCMgrDaemon", "QQPCUpdate", "ztsmwechat",
)
SYSTEM_DATA = (
    "/Library/Application Support/QQPCMgrBackup",
    "/Library/Application Support/QQPCMgrPKGBackup",
    "/Library/Application Support/QQPCiOA",
    "/Library/Application Support/com.tencent.smartvpn",
    "/Library/Application Support/.ztsmconfig",
    "/Library/Application Support/iOAGuarderLog",
    "/Library/Logs/QQPCMgr", "/Library/Logs/iOADiagnosis",
    "/Library/Logs/com.tencent.smartvpn", "/Library/Logs/ztsmngn",
    "/Users/.AllUsers/iOACloud",
)
TEMP_NAMES = (
    ".iOA", "iOALog", "iOAUninstallLog", ".ioaxlogmmapfiles",
    "qqpcmgr{81F340E1-5407-4807-BDEA-52C17CE1EEBA}",
    "qqpcmgrdaemon{551B4C20-E32A-437e-9795-B85556883B93}",
    "networkservicemgr{6065B634-ED3B-4afd-AA64-79BB7776BCA8}",
    "qqpcpolicymgr{0D745BEE-314B-4dda-ACC5-C09BE0A9CCB1}",
    "ztsmwechat{F2D4BE14-4BF7-4CE1-833A-89F67ECD8A19}",
)


class Refusal(RuntimeError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    executable: str
    started: str


@dataclass(frozen=True)
class Job:
    path: str
    domain: str
    label: str

    @property
    def service(self):
        return self.domain + "/" + self.label


def is_beneath(path, root):
    """Component comparison, not a string-prefix match."""
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def user_account(name=None):
    account = pwd.getpwnam(name) if name else pwd.getpwuid(int(os.environ.get("SUDO_UID", os.getuid())))
    if account.pw_uid == 0:
        raise Refusal("Run sudo from the intended user's session, or specify --user USER.")
    home = Path(account.pw_dir)
    if not home.is_absolute() or home in (Path("/"), Path("/Users"), Path("/Library")):
        raise Refusal("Unexpected user home directory.")
    return account


def candidate_paths(root, home):
    targets = [root, HELPER] + [Path(p) for p in SYSTEM_DATA]
    for library in (Path("/Library"), home / "Library", Path("/private/var/root/Library")):
        for name in DATA_NAMES:
            targets.append(library / "Preferences" / (name + ".plist"))
            for folder in ("Caches", "HTTPStorages", "WebKit"):
                targets.append(library / folder / name)
            targets.append(library / "HTTPStorages" / (name + ".binarycookies"))
            targets.append(library / "Saved Application State" / (name + ".savedState"))
        targets.append(library / "Application Support/iOACloudL")
    targets.append(home / "Library/Application Support/ZtsmDownloads")
    targets.extend(Path("/private/tmp") / name for name in TEMP_NAMES)
    return list(dict.fromkeys(targets))


def check_path(path):
    """Reject symlinked targets/ancestors and mounted trees before touching them."""
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise Refusal("Symlink in target path: " + str(ancestor))
    if os.path.ismount(path):
        raise Refusal("Mounted target: " + str(path))
    total = info.st_size if not path.is_dir() else 0
    if path.is_dir():
        def fail(error):
            raise error
        for folder, dirs, files in os.walk(path, followlinks=False, onerror=fail):
            for name in dirs + files:
                child = Path(folder) / name
                item = child.lstat()
                if stat.S_ISLNK(item.st_mode):
                    continue  # rmtree unlinks internal links; it does not follow them.
                if item.st_dev != info.st_dev or os.path.ismount(child):
                    raise Refusal("Nested mount: " + str(child))
                if name in files:
                    total += item.st_size
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "bytes": total}


def parse_job(path, raw, root, domain):
    try:
        data = plistlib.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("plist root is not a dictionary")
    except Exception as error:
        if path.stem in LABELS or str(root).encode() in raw or str(HELPER).encode() in raw:
            raise Refusal("Malformed iOA launch configuration: " + str(path)) from error
        return None
    label = data.get("Label", "")
    arguments = data.get("ProgramArguments", [])
    arguments = arguments if isinstance(arguments, list) else []
    executable = data.get("Program") or (arguments[0] if arguments else "")
    owned = isinstance(executable, str) and (is_beneath(executable, root) or is_beneath(executable, HELPER))
    if not owned:
        if label in LABELS:
            raise Refusal("Known iOA label points outside its application: " + str(path))
        return None
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise Refusal("Invalid launch service label: " + str(path))
    return Job(str(path), domain, label)


def process_table():
    """Use the kernel executable path; never select a process by its display name."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    listing = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,lstart="], text=True)
    result = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 7:
            continue
        pid, parent = int(fields[0]), int(fields[1])
        buffer = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, buffer, len(buffer)) > 0:
            result[pid] = Process(pid, parent, os.fsdecode(buffer.value), " ".join(fields[2:]))
    return result


def select_processes(table, root):
    selected = {pid for pid, p in table.items()
                if is_beneath(p.executable, root) or is_beneath(p.executable, HELPER)}
    while True:
        children = {pid for pid, p in table.items() if p.parent in selected}
        if children <= selected:
            break
        selected |= children
    return {pid: table[pid] for pid in selected if pid not in (0, 1, os.getpid(), os.getppid())}


def same_process(before, now):
    return now is not None and (before.pid, before.executable, before.started) == (
        now.pid, now.executable, now.started)


class Uninstaller:
    def __init__(self, root, account, report_dir):
        self.root = root
        self.account = account
        self.home = Path(account.pw_dir)
        self.report_dir = report_dir
        self.jobs = []
        self.targets = []
        self.frozen = {}
        self.report = {"tool_version": VERSION, "status": "inspecting", "removed": [],
                       "blockers": [], "warnings": [], "commands": []}

    def save(self):
        self.report["updated_at"] = datetime.datetime.now().astimezone().isoformat()
        temporary = self.report_dir / "result.tmp"
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.report_dir / "result.json")

    def run(self, args, timeout=30):
        environment = dict(os.environ, LC_ALL="C", LANG="C")
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout, env=environment)
        self.report["commands"].append({"args": args, "returncode": result.returncode,
                                        "output": result.stdout})
        return result

    def required(self, args):
        result = self.run(args)
        if result.returncode:
            raise Refusal("Inspection command failed: " + " ".join(args))
        return result.stdout

    def verify_signature(self, path):
        self.required(["/usr/bin/codesign", "--verify", "--strict", str(path)])
        output = self.required(["/usr/bin/codesign", "-d", "--verbose=4", str(path)])
        if "TeamIdentifier=" + TEAM_ID not in output.splitlines():
            raise Refusal("Unexpected signing team: " + str(path))

    def processes(self):
        return select_processes(process_table(), self.root)

    def signal(self, process, number):
        # A PID can be recycled between inventory and shutdown.
        if same_process(process, process_table().get(process.pid)):
            try:
                os.kill(process.pid, number)
            except ProcessLookupError:
                pass

    def snapshot_network(self, name):
        self.report[name] = {
            "proxy": self.required(["/usr/sbin/scutil", "--proxy"]),
            "dns": self.required(["/usr/sbin/scutil", "--dns"]),
        }

    def discover_jobs(self):
        jobs = []
        uid = str(self.account.pw_uid)
        locations = [(Path("/Library/LaunchDaemons"), "system"),
                     (Path("/Library/LaunchAgents"), "gui/" + uid),
                     (self.home / "Library/LaunchAgents", "gui/" + uid)]
        for folder, domain in locations:
            if not folder.exists():
                continue
            # iterdir propagates permission errors; glob may silently hide them.
            for path in sorted(folder.iterdir()):
                if path.suffix != ".plist":
                    continue
                job = parse_job(path, path.read_bytes(), self.root, domain)
                if job:
                    jobs.append(job)
        # A launchd registration can survive deletion of its backing plist.
        expected = [("system", "/Library/LaunchDaemons", label) for label in (
            "com.tencent.runmgr", "com.tencent.QQPCGuardMonitor", "com.tencent.iOAPolicyMgr.Daemon")]
        expected += [("gui/" + uid, "/Library/LaunchAgents", label) for label in (
            "com.tencent.QQPCMgr", "com.tencent.iOAWatermark.Daemon", "com.tencent.PreLoginQQPCMgr.Daemon")]
        for domain, folder, label in expected:
            job = Job(str(Path(folder) / (label + ".plist")), domain, label)
            if any(j.service == job.service for j in jobs):
                continue
            probe = self.run(["/bin/launchctl", "print", job.service], timeout=5)
            if probe.returncode:
                if "Could not find service" in probe.stdout or "Could not find domain" in probe.stdout:
                    continue
                raise Refusal("Cannot inspect launch service: " + job.service)
            match = re.search(r"^\s*program = (.+)$", probe.stdout, re.MULTILINE)
            if not match or not any(is_beneath(match[1], p) for p in (self.root, HELPER)):
                raise Refusal("Registered service has an unexpected executable: " + job.service)
            jobs.append(job)
        return jobs

    def user_cache_paths(self):
        if os.geteuid() != 0:
            return []
        paths = []
        for uid in (0, self.account.pw_uid):
            value = self.required(["/usr/bin/sudo", "-n", "-u", "#" + str(uid),
                                   "/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"]).strip()
            cache = Path(value).resolve()
            if not is_beneath(cache, Path("/private/var/folders")) or cache == Path("/private/var/folders"):
                raise Refusal("Unexpected Darwin cache directory: " + value)
            paths.extend(cache / name for name in ("com.tencent.QQPCMgr", "Tencent.Uninstaller"))
        return paths

    def preflight(self):
        if os.geteuid() != 0:
            self.report["blockers"].append("Root inspection is required for a complete inventory; rerun with sudo.")
        for other in APP_ROOTS:
            if other != self.root and other.exists():
                raise Refusal("Another installation root exists; inspect it separately: " + str(other))
        # The main bundle must exist and pass signature/identity checks before --apply.
        bundle = self.root / "QQPCMgr.app"
        if bundle.exists():
            check_path(self.root)
            info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
            if info.get("CFBundleIdentifier") != "com.tencent.QQPCMgr":
                raise Refusal("Unexpected application bundle identifier.")
            version = info.get("CFBundleShortVersionString", "")
            self.report["installed_version"] = version
            if not isinstance(version, str) or not version.startswith(("211.5.", "211.6.")):
                raise Refusal("Unreviewed iOA version; no automatic removal: " + str(version))
            self.verify_signature(bundle)
        else:
            self.report["blockers"].append("Main iOA bundle absent: residual inspection only; automatic removal unavailable.")
        extensions = self.required(["/usr/bin/systemextensionsctl", "list"])
        if any(marker in extensions.lower() for marker in PRODUCT_MARKERS):
            raise Refusal("An iOA system extension is registered; use supported deactivation first.")
        enrollment = self.required(["/usr/bin/profiles", "status", "-type", "enrollment"])
        if not all(line in enrollment for line in ("Enrolled via DEP: No", "MDM enrollment: No")):
            raise Refusal("Managed or unknown enrollment state is outside this tool's scope.")
        authorization = self.required(["/usr/bin/security", "authorizationdb", "read", "system.login.console"])
        if any(marker in authorization.lower() for marker in PRODUCT_MARKERS):
            raise Refusal("An iOA login mechanism needs separate restoration before removal.")
        for name in ("/etc/pf.anchors/com.QQPCMgr.out-firewall", "/etc/pf.anchors/com.EDR.out-firewall", "/etc/isolation.conf"):
            if os.path.lexists(name):
                raise Refusal("Isolation/firewall configuration needs separate restoration: " + name)
        self.snapshot_network("network_before")

    def inspect(self):
        try:
            self.preflight()
        except (OSError, ValueError, Refusal, subprocess.SubprocessError) as error:
            self.report["blockers"].append(str(error))
        try:
            self.jobs = self.discover_jobs()
        except (OSError, Refusal) as error:
            self.report["blockers"].append(str(error))
        candidates = candidate_paths(self.root, self.home) + [Path(j.path) for j in self.jobs]
        try:
            candidates += self.user_cache_paths()
        except (OSError, Refusal, subprocess.SubprocessError) as error:
            self.report["blockers"].append(str(error))
        for path in dict.fromkeys(candidates):
            try:
                target = check_path(path)
                if target:
                    self.targets.append(target)
            except (OSError, Refusal) as error:
                self.report["blockers"].append(str(error))
        self.report["targets"] = self.targets
        self.report["jobs"] = [asdict(j) for j in self.jobs]
        self.report["processes_before"] = [asdict(p) for p in self.processes().values()]
        self.report["estimated_file_bytes"] = sum(t["bytes"] for t in self.targets)
        self.report["status"] = "inspection_blocked" if self.report["blockers"] else "inspection_ready"
        self.save()

    def service_present(self, job):
        result = self.run(["/bin/launchctl", "print", job.service], timeout=5)
        if result.returncode == 0:
            return True
        # A permission/IPC failure must not be treated as successful unregistration.
        if "Could not find service" in result.stdout or "Could not find domain" in result.stdout:
            return False
        raise Refusal("Unable to determine service state: " + job.service)

    def bootout(self, job):
        if not self.service_present(job):
            return
        self.run(["/bin/launchctl", "bootout", job.service], timeout=25)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not self.service_present(job):
                return
            time.sleep(0.25)
        raise Refusal("Service has not unregistered: " + job.service)

    def stop_vpn(self):
        def active():
            return [p for p in self.processes().values() if Path(p.executable).name == "ztsmngn"]
        if not active():
            return
        tool = self.root / "QQPCMgr.app/Contents/TPN/SmartVPNTool"
        check_path(tool)
        self.verify_signature(tool)
        result = self.run([str(tool), "tpn", "stop"], timeout=25)
        if result.returncode:
            self.report["warnings"].append("VPN cleanup returned nonzero; checking whether its process actually stopped.")
        time.sleep(1)
        for process in active():
            self.signal(process, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while active() and time.monotonic() < deadline:
            time.sleep(0.25)
        if active():
            raise Refusal("iOA VPN is still running; application files have not been removed.")

    def stop(self):
        self.report["status"] = "stopping"
        self.save()
        tracked = self.processes()
        for process in tracked.values():
            if Path(process.executable).name in SUPERVISORS:
                self.frozen[process.pid] = process
                self.signal(process, signal.SIGSTOP)
        self.stop_vpn()
        for job in self.jobs:
            self.bootout(job)
        for process in self.frozen.values():
            self.signal(process, signal.SIGKILL)
        self.frozen.clear()
        tracked.update(self.processes())
        for process in tracked.values():
            self.signal(process, signal.SIGTERM)
            self.signal(process, signal.SIGCONT)
        time.sleep(2)
        tracked.update(self.processes())
        for process in tracked.values():
            self.signal(process, signal.SIGKILL)
        time.sleep(1)
        table = process_table()
        if self.processes() or any(same_process(p, table.get(p.pid)) for p in tracked.values()):
            raise Refusal("iOA processes remain; application files have not been removed.")
        if any(self.service_present(j) for j in self.jobs):
            raise Refusal("An iOA service was restored; application files have not been removed.")

    def delete_targets(self):
        self.report["status"] = "removing"
        self.save()
        for original in self.targets:
            path = Path(original["path"])
            current = check_path(path)
            if current is None:
                continue
            if (current["device"], current["inode"]) != (original["device"], original["inode"]):
                raise Refusal("Target replaced since inspection: " + str(path))
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            self.report["removed"].append(str(path))
            self.save()

    def verify(self):
        self.run(["/usr/sbin/pkgutil", "--forget", RECEIPT])
        time.sleep(3)
        remaining = [t["path"] for t in self.targets if os.path.lexists(t["path"])]
        processes = [asdict(p) for p in self.processes().values()]
        services = [j.service for j in self.jobs if self.service_present(j)]
        receipt = self.run(["/usr/sbin/pkgutil", "--pkg-info", RECEIPT])
        receipt_present = receipt.returncode == 0
        if receipt.returncode and "No receipt for" not in receipt.stdout:
            raise Refusal("Cannot verify installer receipt state.")
        self.snapshot_network("network_after")
        self.report.update(remaining=remaining, remaining_processes=processes,
                           remaining_services=services, installer_receipt_present=receipt_present,
                           proxy_unchanged=self.report["network_before"]["proxy"] == self.report["network_after"]["proxy"])
        if remaining or processes or services or receipt_present:
            raise Refusal("Verification found remaining components; see result.json.")
        self.report["status"] = "complete_for_listed_targets"

    def apply(self):
        if os.geteuid() != 0 or self.report["blockers"]:
            raise Refusal("Resolve all inspection blockers before --apply.")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise Refusal("This Python lacks fd-based directory removal; use a supported Python build.")
        try:
            self.stop()
            self.delete_targets()
            self.verify()
        finally:
            # On abort, release any still-existing process that we suspended.
            # Services already removed and files already deleted are NOT rolled back.
            for process in self.frozen.values():
                self.signal(process, signal.SIGCONT)
            self.frozen.clear()
            self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description="iOA macOS uninstaller — inspection by default / 默认只检查")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="inspect only (default)")
    mode.add_argument("--apply", action="store_true", help="stop iOA and permanently remove the listed targets")
    parser.add_argument("--user", help="local account whose iOA data should be inspected/removed")
    parser.add_argument("--app-root", choices=[str(p) for p in APP_ROOTS], default=str(APP_ROOTS[0]))
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("This tool supports macOS only.")
    def interrupted(number, frame):
        raise KeyboardInterrupt("Received signal " + str(number))
    previous_term_handler = signal.signal(signal.SIGTERM, interrupted)
    worker = None
    try:
        account = user_account(args.user)
        os.umask(0o077)
        reports = Path(tempfile.mkdtemp(prefix="ioa-uninstaller-", dir="/private/var/tmp"))
        worker = Uninstaller(Path(args.app_root), account, reports)
        print("检查 iOA 安装与精确删除范围 / Inspecting iOA…", flush=True)
        worker.inspect()
        for target in worker.targets:
            print("  " + target["path"])
        print("文件大小估计 / Estimated file bytes:", worker.report["estimated_file_bytes"])
        for blocker in worker.report["blockers"]:
            print("阻止自动卸载 / Blocker:", blocker)
        if args.apply:
            print("执行卸载 / Applying removal…", flush=True)
            worker.apply()
        else:
            print("仅检查：没有停止服务或删除文件 / Inspection only; no uninstall actions.")
        print("状态 / Status:", worker.report["status"])
        print("报告 / Report:", reports / "result.json")
        return 2 if worker.report["blockers"] else 0
    except (Exception, KeyboardInterrupt) as error:
        if worker is not None:
            worker.report["status"] = "failed"
            worker.report["error"] = str(error) or "Interrupted"
            worker.save()
            print("报告 / Report:", worker.report_dir / "result.json")
        print("已停止 / Stopped:", str(error) or "Interrupted", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term_handler)


if __name__ == "__main__":
    sys.exit(main())

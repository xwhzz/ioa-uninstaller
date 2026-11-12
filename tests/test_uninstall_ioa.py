import io
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import uninstall_ioa as u


def completed(code=0, output=""):
    return subprocess.CompletedProcess([], code, output)


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_component_boundary(self):
        self.assertTrue(u.is_beneath("/Applications/QQPCMgr.localized/QQPCMgr.app", u.APP_ROOTS[0]))
        self.assertFalse(u.is_beneath("/Applications/QQPCMgr.localized-other/QQPCMgr.app", u.APP_ROOTS[0]))

    def test_exact_candidates_exclude_shared_tencent_and_personal_downloads(self):
        home = self.base / "user"
        paths = u.candidate_paths(u.APP_ROOTS[0], home)
        self.assertNotIn(home / "Downloads", paths)
        self.assertNotIn(home / "Library/Application Support/Tencent", paths)
        self.assertNotIn(home / "Library/Containers/com.tencent.xinWeChat", paths)
        self.assertIn(Path("/Library/Application Support/iOAGuarderLog"), paths)
        self.assertIn(home / "Library/Preferences/com.tencent.QQPCMgr.plist", paths)

    def test_refuses_symlink_target_and_ancestor(self):
        real = self.base / "real"
        real.mkdir()
        (real / "file").write_text("keep")
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)
        for target in (link, link / "file"):
            with self.subTest(target=target), self.assertRaises(u.Refusal):
                u.check_path(target)

    def test_refuses_nested_mount(self):
        folder = self.base / "data"
        child = folder / "mounted"
        child.mkdir(parents=True)
        with patch.object(u.os.path, "ismount", side_effect=lambda p: Path(p) == child):
            with self.assertRaises(u.Refusal):
                u.check_path(folder)

    def test_internal_symlink_does_not_delete_destination(self):
        outside = self.base / "outside"
        outside.write_text("keep")
        target = self.base / "data"
        target.mkdir()
        (target / "link").symlink_to(outside)
        worker = make_worker(self.base)
        worker.targets = [u.check_path(target)]
        worker.delete_targets()
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse(target.exists())

    def test_replaced_target_refused(self):
        target = self.base / "data"
        target.write_text("original")
        worker = make_worker(self.base)
        worker.targets = [u.check_path(target)]
        target.rename(self.base / "original")
        target.write_text("replacement")
        with self.assertRaises(u.Refusal):
            worker.delete_targets()
        self.assertEqual(target.read_text(), "replacement")


class JobTests(unittest.TestCase):
    def test_ignores_unrelated_malformed_plist(self):
        self.assertIsNone(u.parse_job(Path("other.plist"), b"<broken & xml>", u.APP_ROOTS[0], "system"))

    def test_refuses_malformed_known_plist(self):
        with self.assertRaises(u.Refusal):
            u.parse_job(Path("com.tencent.runmgr.plist"), b"<broken & xml>", u.APP_ROOTS[0], "system")

    def test_refuses_known_label_pointing_to_another_app(self):
        raw = plistlib.dumps({"Label": "com.tencent.runmgr", "Program": "/Applications/Other.app/runmgr"})
        with self.assertRaises(u.Refusal):
            u.parse_job(Path("com.tencent.runmgr.plist"), raw, u.APP_ROOTS[0], "system")

    def test_argument_mention_is_not_executable_ownership(self):
        raw = plistlib.dumps({"Label": "other", "ProgramArguments": ["/bin/echo", str(u.APP_ROOTS[0])]})
        self.assertIsNone(u.parse_job(Path("other.plist"), raw, u.APP_ROOTS[0], "system"))

    def test_owned_job_selected_and_label_validated(self):
        data = {"Label": "com.tencent.runmgr", "ProgramArguments": [str(u.APP_ROOTS[0] / "QQPCMgr.app/runmgr")]}
        self.assertEqual(u.parse_job(Path("job.plist"), plistlib.dumps(data), u.APP_ROOTS[0], "system").service,
                         "system/com.tencent.runmgr")
        data["Label"] = "../other"
        with self.assertRaises(u.Refusal):
            u.parse_job(Path("job.plist"), plistlib.dumps(data), u.APP_ROOTS[0], "system")


def make_worker(base):
    return u.Uninstaller(u.APP_ROOTS[0], SimpleNamespace(pw_uid=501, pw_dir=str(base / "user")), base)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.worker = make_worker(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def test_selects_paths_and_descendants_not_bare_names(self):
        table = {
            20001: u.Process(20001, 1, str(u.APP_ROOTS[0] / "QQPCMgr.app/runmgr"), "start"),
            20002: u.Process(20002, 20001, "/bin/sh", "start"),
            20003: u.Process(20003, 1, "/Applications/Other.app/runmgr", "start"),
        }
        self.assertEqual(set(u.select_processes(table, u.APP_ROOTS[0])), {20001, 20002})

    def test_recycled_pid_not_signaled(self):
        old = u.Process(20001, 1, "/app/runmgr", "old")
        new = u.Process(20001, 1, "/app/runmgr", "new")
        with patch.object(u, "process_table", return_value={20001: new}), patch.object(u.os, "kill") as kill:
            self.worker.signal(old, signal.SIGKILL)
        kill.assert_not_called()

    def test_permission_error_is_not_absent_service(self):
        with patch.object(self.worker, "run", return_value=completed(1, "Operation not permitted")):
            with self.assertRaises(u.Refusal):
                self.worker.service_present(u.Job("job.plist", "system", "com.tencent.runmgr"))

    def test_registered_service_found_without_backing_plist(self):
        def probe(args, timeout=30):
            if args[-1] == "system/com.tencent.runmgr":
                return completed(0, "\tprogram = /Applications/QQPCMgr.localized/QQPCMgr.app/Contents/Resources/runmgr\n")
            return completed(113, "Could not find service")
        with patch.object(Path, "exists", return_value=False), patch.object(self.worker, "run", side_effect=probe):
            jobs = self.worker.discover_jobs()
        self.assertEqual([j.service for j in jobs], ["system/com.tencent.runmgr"])

    def test_registered_foreign_service_refused(self):
        with patch.object(Path, "exists", return_value=False), \
                patch.object(self.worker, "run", return_value=completed(0, "\tprogram = /Applications/Other.app/runmgr\n")):
            with self.assertRaises(u.Refusal):
                self.worker.discover_jobs()

    def test_wrong_signing_team_refused(self):
        with patch.object(self.worker, "required", side_effect=["", "TeamIdentifier=OTHERTEAM\n"]):
            with self.assertRaises(u.Refusal):
                self.worker.verify_signature(self.base / "app")

    def test_bootout_waits_for_async_unregistration(self):
        with patch.object(self.worker, "service_present", side_effect=[True, True, False]) as present, \
                patch.object(self.worker, "run") as run, patch.object(u.time, "sleep"):
            self.worker.bootout(u.Job("job.plist", "system", "com.tencent.runmgr"))
        self.assertEqual(present.call_count, 3)
        run.assert_called_once_with(["/bin/launchctl", "bootout", "system/com.tencent.runmgr"], timeout=25)

    def test_nonzero_vpn_cleanup_accepted_only_after_process_exits(self):
        vpn = u.Process(20001, 1, str(u.APP_ROOTS[0] / "QQPCMgr.app/Contents/TPN/ztsmngn"), "start")
        with patch.object(self.worker, "processes", side_effect=[{vpn.pid: vpn}, {}, {}, {}]), \
                patch.object(self.worker, "verify_signature"), patch.object(u, "check_path"), \
                patch.object(self.worker, "run", return_value=completed(1, "connection reset")), \
                patch.object(u.time, "sleep"):
            self.worker.stop_vpn()
        self.assertEqual(len(self.worker.report["warnings"]), 1)

    def test_vpn_still_running_aborts(self):
        vpn = u.Process(20001, 1, str(u.APP_ROOTS[0] / "QQPCMgr.app/Contents/TPN/ztsmngn"), "start")
        with patch.object(self.worker, "processes", return_value={vpn.pid: vpn}), \
                patch.object(self.worker, "verify_signature"), patch.object(u, "check_path"), \
                patch.object(self.worker, "signal"), patch.object(self.worker, "run", return_value=completed()), \
                patch.object(u.time, "sleep"), patch.object(u.time, "monotonic", side_effect=[0, 10]):
            with self.assertRaises(u.Refusal):
                self.worker.stop_vpn()

    def test_stop_failure_prevents_deletion_and_resumes_frozen_process(self):
        process = u.Process(20001, 1, "/app/runmgr", "start")
        self.worker.frozen[process.pid] = process
        with patch.object(u.os, "geteuid", return_value=0), \
                patch.object(u.shutil.rmtree, "avoids_symlink_attacks", True), \
                patch.object(self.worker, "stop", side_effect=u.Refusal("stop failed")), \
                patch.object(self.worker, "delete_targets") as delete, patch.object(self.worker, "signal") as send:
            with self.assertRaises(u.Refusal):
                self.worker.apply()
        delete.assert_not_called()
        send.assert_called_once_with(process, signal.SIGCONT)

    def test_apply_requires_root_and_clear_inspection(self):
        with patch.object(u.os, "geteuid", return_value=0), patch.object(self.worker, "stop") as stop:
            self.worker.report["blockers"].append("unknown extension")
            with self.assertRaises(u.Refusal):
                self.worker.apply()
        stop.assert_not_called()

    def test_default_cli_does_not_apply(self):
        account = SimpleNamespace(pw_uid=501, pw_dir=str(self.base / "user"))
        with patch.object(u.sys, "platform", "darwin"), patch.object(u, "user_account", return_value=account), \
                patch.object(u.os, "umask"), patch.object(u.tempfile, "mkdtemp", return_value=str(self.base)), \
                patch.object(u.Uninstaller, "inspect"), patch.object(u.Uninstaller, "apply") as apply, \
                patch("sys.stdout", new_callable=io.StringIO):
            # Simulate an inspection with no targets, without invoking system commands.
            with patch.object(u.Uninstaller, "__init__", return_value=None):
                with patch.object(u.Uninstaller, "targets", [], create=True), \
                        patch.object(u.Uninstaller, "report", {"estimated_file_bytes": 0, "blockers": [], "status": "inspection_ready"}, create=True):
                    self.assertEqual(u.main([]), 0)
        apply.assert_not_called()

    def test_network_commands_are_read_only(self):
        with patch.object(self.worker, "required", return_value="snapshot") as run:
            self.worker.snapshot_network("network_before")
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["/usr/sbin/scutil", "--proxy"], ["/usr/sbin/scutil", "--dns"]])

    def test_remaining_file_prevents_complete_status(self):
        target = self.base / "remaining"
        target.write_text("not removed")
        self.worker.targets = [u.check_path(target)]
        self.worker.report["network_before"] = {"proxy": "same"}
        self.worker.report["network_after"] = {"proxy": "same"}
        with patch.object(self.worker, "run", return_value=completed(1, "No receipt for com.tencent.iOA")), \
                patch.object(self.worker, "processes", return_value={}), \
                patch.object(self.worker, "snapshot_network"), patch.object(u.time, "sleep"):
            with self.assertRaises(u.Refusal):
                self.worker.verify()
        self.assertEqual(self.worker.report["remaining"], [str(target)])
        self.assertNotEqual(self.worker.report["status"], "complete_for_listed_targets")

    def test_unknown_receipt_error_prevents_success(self):
        with patch.object(self.worker, "run", return_value=completed(1, "database inaccessible")), \
                patch.object(self.worker, "processes", return_value={}), patch.object(u.time, "sleep"):
            with self.assertRaises(u.Refusal):
                self.worker.verify()
        self.assertNotEqual(self.worker.report["status"], "complete_for_listed_targets")


if __name__ == "__main__":
    unittest.main()

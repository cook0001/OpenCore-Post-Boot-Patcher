import importlib
import json
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_linux_stubs_if_needed() -> dict[str, types.ModuleType]:
    """
    Provide minimal stubs for macOS-only or optional third-party modules
    when running unit tests on non-macOS hosts.
    """
    added: dict[str, types.ModuleType] = {}

    def _ensure_stub(name: str, setup=None) -> types.ModuleType:
        if name in sys.modules:
            return sys.modules[name]
        try:
            return importlib.import_module(name)
        except ImportError:
            mod = types.ModuleType(name)
            if setup:
                setup(mod)
            sys.modules[name] = mod
            added[name] = mod
            return mod

    def _setup_objc(mod: types.ModuleType) -> None:
        mod.loadBundleFunctions = lambda *args, **kwargs: None
        mod.loadBundleVariables = lambda *args, **kwargs: None

    def _setup_cf(mod: types.ModuleType) -> None:
        mod.CFRelease = lambda *args, **kwargs: None
        mod.kCFAllocatorDefault = object()

    def _setup_foundation(mod: types.ModuleType) -> None:
        class _NSBundle:
            @staticmethod
            def bundleWithIdentifier_(_identifier: str):
                return object()

        mod.NSBundle = _NSBundle

    def _setup_pyobjc_tools(mod: types.ModuleType) -> None:
        conv = types.ModuleType("PyObjCTools.Conversion")
        conv.pythonCollectionFromPropertyList = lambda x: x
        conv.propertyListFromPythonCollection = lambda x: x
        mod.Conversion = conv
        sys.modules["PyObjCTools.Conversion"] = conv
        added["PyObjCTools.Conversion"] = conv

    def _setup_sip(mod: types.ModuleType) -> None:
        class _SipXnu:
            def get_sip_status(self):
                return types.SimpleNamespace(value=0)

        mod.SipXnu = _SipXnu

    def _setup_requests(mod: types.ModuleType) -> None:
        class _RequestException(Exception):
            pass

        class _Session:
            pass

        class _Response:
            pass

        exceptions = types.ModuleType("requests.exceptions")
        exceptions.Timeout = _RequestException
        exceptions.TooManyRedirects = _RequestException
        exceptions.ConnectionError = _RequestException
        exceptions.HTTPError = _RequestException
        mod.exceptions = exceptions
        mod.Session = _Session
        mod.Response = _Response
        mod.get = lambda *args, **kwargs: None
        mod.head = lambda *args, **kwargs: None
        sys.modules["requests.exceptions"] = exceptions
        added["requests.exceptions"] = exceptions

    _ensure_stub("objc", _setup_objc)
    _ensure_stub("CoreFoundation", _setup_cf)
    _ensure_stub("Foundation", _setup_foundation)
    _ensure_stub("PyObjCTools", _setup_pyobjc_tools)
    _ensure_stub("py_sip_xnu", _setup_sip)
    _ensure_stub("requests", _setup_requests)
    return added


_STUBS = _install_linux_stubs_if_needed()

from opencore_legacy_patcher import constants
from opencore_legacy_patcher.detections import device_probe
from opencore_legacy_patcher.support import (
    global_settings,
    logging_handler,
    subprocess_wrapper,
    updates,
    utilities,
)


class HeadlessImportIsolationTests(unittest.TestCase):
    def test_non_gui_modules_import_without_wx_or_applescript(self) -> None:
        repo_root = str(Path(__file__).resolve().parent.parent)
        script = f"""
import importlib
import sys
import types

FORBIDDEN = ("wx", "applescript", "markdown2", "opencore_legacy_patcher.wx_gui")

class _BlockGuiImports:
    def find_spec(self, fullname, path, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in FORBIDDEN):
            raise AssertionError(f"Forbidden GUI import during headless load: {{fullname}}")
        return None

sys.meta_path.insert(0, _BlockGuiImports())
sys.path.insert(0, {repo_root!r})

def _stub_if_missing(name, attrs=None):
    try:
        importlib.import_module(name)
    except ImportError:
        mod = types.ModuleType(name)
        for k, v in (attrs or {{}}).items():
            setattr(mod, k, v)
        sys.modules[name] = mod

_stub_if_missing("objc", {{"loadBundleFunctions": lambda *a, **kw: None, "loadBundleVariables": lambda *a, **kw: None}})
_stub_if_missing("CoreFoundation", {{"CFRelease": lambda *a, **kw: None, "kCFAllocatorDefault": object()}})
_stub_if_missing("Foundation", {{"NSBundle": type("NSBundle", (), {{"bundleWithIdentifier_": staticmethod(lambda _: object())}})}})
_conv = types.ModuleType("PyObjCTools.Conversion")
_conv.pythonCollectionFromPropertyList = lambda x: x
_conv.propertyListFromPythonCollection = lambda x: x
_stub_if_missing("PyObjCTools", {{"Conversion": _conv}})
if "PyObjCTools.Conversion" not in sys.modules:
    sys.modules["PyObjCTools.Conversion"] = _conv
_stub_if_missing("py_sip_xnu", {{"SipXnu": type("SipXnu", (), {{"get_sip_status": lambda self: types.SimpleNamespace(value=0)}})}})

class _ReqExc(Exception):
    pass
_req_exc = types.ModuleType("requests.exceptions")
_req_exc.Timeout = _ReqExc
_req_exc.TooManyRedirects = _ReqExc
_req_exc.ConnectionError = _ReqExc
_req_exc.HTTPError = _ReqExc
_stub_if_missing("requests", {{"Session": object, "Response": object, "exceptions": _req_exc, "get": lambda *a, **kw: None, "head": lambda *a, **kw: None}})
if "requests.exceptions" not in sys.modules:
    sys.modules["requests.exceptions"] = _req_exc

modules = [
    "opencore_legacy_patcher.constants",
    "opencore_legacy_patcher.application_entry",
    "opencore_legacy_patcher.support.arguments",
    "opencore_legacy_patcher.support.global_settings",
    "opencore_legacy_patcher.support.logging_handler",
    "opencore_legacy_patcher.support.subprocess_wrapper",
    "opencore_legacy_patcher.support.updates",
    "opencore_legacy_patcher.sys_patch.utilities.dmg_mount",
    "opencore_legacy_patcher.sys_patch.auto_patcher",
    "opencore_legacy_patcher.sys_patch.sys_patch",
    "opencore_legacy_patcher.efi_builder.build",
]
for mod_name in modules:
    importlib.import_module(mod_name)
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class RepoConstantsAndUpdatesTests(unittest.TestCase):
    def test_constants_point_to_ocpbp_repo_and_preserve_psp(self) -> None:
        c = constants.Constants()
        self.assertEqual(c.repo_slug, "cook0001/OpenCore-Post-Boot-Patcher")
        self.assertEqual(c.repo_link, "https://github.com/cook0001/OpenCore-Post-Boot-Patcher")
        self.assertEqual(
            c.repo_releases_api,
            "https://api.github.com/repos/cook0001/OpenCore-Post-Boot-Patcher/releases/latest",
        )
        self.assertEqual(
            c.repo_branches_api,
            "https://api.github.com/repos/cook0001/OpenCore-Post-Boot-Patcher/branches",
        )
        self.assertTrue(c.installer_pkg_url.startswith(c.repo_link))
        self.assertIn("cook0001/OpenCore-Post-Boot-Patcher", c.installer_pkg_url_nightly)
        self.assertEqual(
            c.url_patcher_support_pkg,
            "https://github.com/dortania/PatcherSupportPkg/releases/download/",
        )

    def test_updates_uses_ocpbp_repo_urls(self) -> None:
        c = constants.Constants()
        c.patcher_version = "3.0.0"
        c.commit_info = ("refs/tags/3.0.0", "2026-11-18", "")

        fake_resp = mock.MagicMock()
        fake_resp.json.return_value = {
            "tag_name": "3.1.0",
            "assets": [
                {
                    "name": "OpenCore-Patcher.pkg",
                    "browser_download_url": "https://github.com/cook0001/OpenCore-Post-Boot-Patcher/releases/download/3.1.0/OpenCore-Patcher.pkg",
                }
            ],
        }

        with mock.patch.object(updates.network_handler.NetworkUtilities, "verify_network_connection", return_value=True) as mock_verify, \
             mock.patch.object(updates.network_handler.NetworkUtilities, "get", return_value=fake_resp) as mock_get:
            details = updates.CheckBinaryUpdates(c).check_binary_updates()

        self.assertIsNotNone(details)
        self.assertEqual(
            details["Github Link"],
            "https://github.com/cook0001/OpenCore-Post-Boot-Patcher/releases/tag/3.1.0",
        )
        mock_verify.assert_called_once()
        mock_get.assert_called_once_with(c.repo_releases_api)


class SubprocessWrapperElevationTests(unittest.TestCase):
    def test_missing_binary_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            subprocess_wrapper.run_as_root(["/nonexistent/binary/path", "--help"])

    def test_euid_zero_runs_command_directly(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp:
            expected = subprocess.CompletedProcess([tmp.name, "--arg"], 0)
            with mock.patch("os.geteuid", return_value=0), \
                 mock.patch("subprocess.run", return_value=expected) as mock_run:
                res = subprocess_wrapper.run_as_root([Path(tmp.name), "--arg"], capture_output=True)

            self.assertIs(res, expected)
            mock_run.assert_called_once_with([tmp.name, "--arg"], capture_output=True)

    def test_helper_command_failure_does_not_fallback(self) -> None:
        with tempfile.NamedTemporaryFile() as cmd_file, tempfile.NamedTemporaryFile() as helper_file:
            helper_fail = subprocess.CompletedProcess([helper_file.name, cmd_file.name], 1)
            with mock.patch.object(subprocess_wrapper, "OCLP_PRIVILEGED_HELPER", helper_file.name), \
                 mock.patch("os.geteuid", return_value=501), \
                 mock.patch("subprocess.run", return_value=helper_fail) as mock_run:
                res = subprocess_wrapper.run_as_root([cmd_file.name, "arg1"])

            self.assertIs(res, helper_fail)
            mock_run.assert_called_once()

    def test_helper_team_id_rejection_falls_back_to_osascript_in_gui(self) -> None:
        with tempfile.NamedTemporaryFile() as cmd_file, \
             tempfile.NamedTemporaryFile() as helper_file, \
             tempfile.NamedTemporaryFile() as osascript_file:
            helper_reject = subprocess.CompletedProcess([helper_file.name], 166)
            osa_ok = subprocess.CompletedProcess([osascript_file.name], 0)

            with mock.patch.object(subprocess_wrapper, "OCLP_PRIVILEGED_HELPER", helper_file.name), \
                 mock.patch.object(subprocess_wrapper, "_OSASCRIPT_PATH", osascript_file.name), \
                 mock.patch.object(subprocess_wrapper, "_is_interactive_tty", return_value=False), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch("os.geteuid", return_value=501), \
                 mock.patch("subprocess.run", side_effect=[helper_reject, osa_ok]) as mock_run:
                res = subprocess_wrapper.run_as_root([cmd_file.name, 'hello "world"', "a b"])

            self.assertIs(res, osa_ok)
            self.assertEqual(mock_run.call_count, 2)
            osa_call_args = mock_run.call_args_list[1].args[0]
            self.assertEqual(osa_call_args[0], osascript_file.name)
            self.assertEqual(osa_call_args[1], "-e")
            self.assertIn("with administrator privileges", osa_call_args[2])
            self.assertIn('\\"world\\"', osa_call_args[2])

    def test_missing_helper_in_cli_tty_falls_back_to_sudo(self) -> None:
        with tempfile.NamedTemporaryFile() as cmd_file, \
             tempfile.NamedTemporaryFile() as sudo_file:
            sudo_ok = subprocess.CompletedProcess([sudo_file.name, cmd_file.name], 0)

            with mock.patch.object(subprocess_wrapper, "OCLP_PRIVILEGED_HELPER", "/nonexistent/helper"), \
                 mock.patch.object(subprocess_wrapper, "_SUDO_PATH", sudo_file.name), \
                 mock.patch.object(subprocess_wrapper, "_is_interactive_tty", return_value=True), \
                 mock.patch.object(sys, "argv", ["OpenCore-Patcher", "--patch_sys_vol"]), \
                 mock.patch("os.geteuid", return_value=501), \
                 mock.patch("subprocess.run", return_value=sudo_ok) as mock_run:
                res = subprocess_wrapper.run_as_root([cmd_file.name, "--flag"])

            self.assertIs(res, sudo_ok)
            mock_run.assert_called_once_with([sudo_file.name, cmd_file.name, "--flag"])

    def test_osascript_non_interactive_error_on_tty_falls_back_to_sudo(self) -> None:
        with tempfile.NamedTemporaryFile() as cmd_file, \
             tempfile.NamedTemporaryFile() as osascript_file, \
             tempfile.NamedTemporaryFile() as sudo_file:
            osa_err = subprocess.CompletedProcess(
                [osascript_file.name],
                1,
                stdout=b"",
                stderr=b"execution error: No user interaction allowed. (-1713)",
            )
            sudo_ok = subprocess.CompletedProcess([sudo_file.name, cmd_file.name], 0)

            with mock.patch.object(subprocess_wrapper, "OCLP_PRIVILEGED_HELPER", "/nonexistent/helper"), \
                 mock.patch.object(subprocess_wrapper, "_OSASCRIPT_PATH", osascript_file.name), \
                 mock.patch.object(subprocess_wrapper, "_SUDO_PATH", sudo_file.name), \
                 mock.patch.object(subprocess_wrapper, "_is_interactive_tty", return_value=True), \
                 mock.patch.object(sys, "argv", ["OpenCore-Patcher"]), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch("os.geteuid", return_value=501), \
                 mock.patch("subprocess.run", side_effect=[osa_err, sudo_ok]) as mock_run:
                res = subprocess_wrapper.run_as_root([cmd_file.name])

            self.assertIs(res, sudo_ok)
            self.assertEqual(mock_run.call_count, 2)

    def test_generate_log_handles_str_and_bytes_outputs(self) -> None:
        proc_str = subprocess.CompletedProcess(["/bin/true"], 1, stdout="out text", stderr="err text")
        log_str = subprocess_wrapper.generate_log(proc_str)
        self.assertIn("out text", log_str)
        self.assertIn("err text", log_str)

        proc_bytes = subprocess.CompletedProcess(["/bin/true"], 166, stdout=b"out bytes", stderr=b"err bytes")
        log_bytes = subprocess_wrapper.generate_log(proc_bytes)
        self.assertIn("OCLP_PHT_ERROR_INVALID_TEAM_ID", log_bytes)
        self.assertIn("out bytes", log_bytes)
        self.assertIn("err bytes", log_bytes)


class RecoverySafetyTests(unittest.TestCase):
    def test_global_settings_missing_directory_does_not_crash(self) -> None:
        missing_dir = "/tmp/ocpbp_nonexistent_shared_dir_for_test"
        settings = global_settings.GlobalEnviromentSettings(global_settings_folder=missing_dir)
        self.assertIsNone(settings.read_property("SomeKey"))
        settings.write_property("SomeKey", True)
        settings.delete_property("SomeKey")
        self.assertIsNone(settings.read_property("SomeKey"))

    def test_global_settings_roundtrip_in_writable_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = global_settings.GlobalEnviromentSettings(global_settings_folder=tmpdir)
            self.assertTrue(Path(settings.global_settings_plist).exists())
            settings.write_property("GUI:sip_status", False)
            self.assertFalse(settings.read_property("GUI:sip_status"))
            settings.delete_property("GUI:sip_status")
            self.assertIsNone(settings.read_property("GUI:sip_status"))

    def test_logging_falls_back_to_tempdir_when_users_shared_missing(self) -> None:
        c = constants.Constants()
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.dict(os.environ, {"HOME": "/nonexistent_home"}), \
             mock.patch("tempfile.gettempdir", return_value=tmpdir):
            orig_handlers = list(logging.getLogger().handlers)
            try:
                logging.getLogger().handlers.clear()
                logger_init = logging_handler.InitializeLoggingSupport(c)
                self.assertEqual(logger_init.log_filepath.parent, Path(tmpdir))
                self.assertTrue(logger_init.log_filepath.exists())
            finally:
                for h in logging.getLogger().handlers:
                    try:
                        h.close()
                    except Exception:
                        pass
                logging.getLogger().handlers[:] = orig_handlers

    def test_sata_disk_probe_handles_missing_or_empty_system_profiler(self) -> None:
        comp = device_probe.Computer()
        with mock.patch("pathlib.Path.exists", return_value=False):
            comp.sata_disk_probe()
        self.assertFalse(comp.third_party_sata_ssd)

        bad_proc = subprocess.CompletedProcess(["/usr/sbin/system_profiler"], 1, stdout=b"")
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("subprocess.run", return_value=bad_proc):
            comp.sata_disk_probe()
        self.assertFalse(comp.third_party_sata_ssd)

    def test_oclp_sys_patch_probe_custom_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            comp = device_probe.Computer()
            comp.oclp_sys_patch_probe(root_path=tmpdir)
            self.assertTrue(comp.oclp_sys_signed)

            cs_dir = Path(tmpdir) / "System/Library/CoreServices"
            cs_dir.mkdir(parents=True)
            plist_path = cs_dir / "OpenCore-Legacy-Patcher.plist"
            with plist_path.open("wb") as f:
                plistlib.dump(
                    {
                        "OpenCore Legacy Patcher": "v3.0.0",
                        "Time Patched": "Nov 20, 2026",
                        "Commit URL": "https://example.com/commit",
                        "Custom Signature": False,
                    },
                    f,
                )

            comp2 = device_probe.Computer()
            comp2.oclp_sys_patch_probe(root_path=tmpdir)
            self.assertEqual(comp2.oclp_sys_version, "v3.0.0")
            self.assertEqual(comp2.oclp_sys_date, "Nov 20, 2026")
            self.assertEqual(comp2.oclp_sys_url, "https://example.com/commit")
            self.assertFalse(comp2.oclp_sys_signed)

    def test_check_boot_mode_handles_missing_system_profiler(self) -> None:
        with mock.patch("pathlib.Path.exists", return_value=False):
            self.assertIsNone(utilities.check_boot_mode())

        bad_proc = subprocess.CompletedProcess(["/usr/sbin/system_profiler"], 0, stdout=b"not-a-plist")
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("subprocess.run", return_value=bad_proc):
            self.assertIsNone(utilities.check_boot_mode())


if __name__ == "__main__":
    unittest.main()

import contextlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_linux_stubs_if_needed() -> None:
    if sys.platform == "darwin":
        return

    if "objc" not in sys.modules:
        objc = types.ModuleType("objc")
        objc.loadBundleFunctions = lambda *args, **kwargs: None
        objc.loadBundleVariables = lambda *args, **kwargs: None
        sys.modules["objc"] = objc

    if "CoreFoundation" not in sys.modules:
        cf = types.ModuleType("CoreFoundation")
        cf.kCFAllocatorDefault = None
        cf.kCFPreferencesAnyHost = None
        cf.kCFPreferencesAnyUser = None
        cf.CFPreferencesCopyValue = lambda *args, **kwargs: None
        cf.CFRelease = lambda *args, **kwargs: None
        sys.modules["CoreFoundation"] = cf

    if "Foundation" not in sys.modules:
        foundation = types.ModuleType("Foundation")
        foundation.NSBundle = type(
            "NSBundle",
            (),
            {
                "bundleWithPath_": classmethod(lambda cls, path: None),
                "bundleWithIdentifier_": classmethod(lambda cls, ident: None),
            },
        )
        foundation.NSUserDefaults = type(
            "NSUserDefaults",
            (),
            {"standardUserDefaults": classmethod(lambda cls: types.SimpleNamespace(dictionaryRepresentation=lambda: {}))},
        )
        sys.modules["Foundation"] = foundation

    if "PyObjCTools" not in sys.modules:
        pyobjctools = types.ModuleType("PyObjCTools")
        conversion = types.ModuleType("PyObjCTools.Conversion")
        conversion.propertyListFromPythonCollection = lambda x: x
        conversion.pythonCollectionFromPropertyList = lambda x: x
        pyobjctools.Conversion = conversion
        sys.modules["PyObjCTools"] = pyobjctools
        sys.modules["PyObjCTools.Conversion"] = conversion

    if "py_sip_xnu" not in sys.modules:
        py_sip_xnu = types.ModuleType("py_sip_xnu")

        class _SipXnu:
            def __init__(self, *args, **kwargs):
                pass

            def get_sip_status(self):
                return types.SimpleNamespace(value=0, can_edit_root=True, can_load_arbitrary_kexts=True)

        py_sip_xnu.SipXnu = _SipXnu
        sys.modules["py_sip_xnu"] = py_sip_xnu

    if "requests" not in sys.modules:
        requests = types.ModuleType("requests")
        exceptions = types.ModuleType("requests.exceptions")

        class RequestException(Exception):
            pass

        exceptions.RequestException = RequestException
        exceptions.Timeout = RequestException
        exceptions.ConnectionError = RequestException
        exceptions.HTTPError = RequestException
        requests.exceptions = exceptions
        requests.RequestException = RequestException
        requests.Response = type("Response", (), {"status_code": 200, "content": b"", "text": "", "headers": {}, "json": lambda self: {}})
        requests.Session = type(
            "Session",
            (),
            {
                "get": lambda self, *a, **kw: requests.Response(),
                "head": lambda self, *a, **kw: requests.Response(),
                "mount": lambda self, *a, **kw: None,
            },
        )
        requests.get = lambda *args, **kwargs: requests.Response()
        requests.head = lambda *args, **kwargs: requests.Response()
        sys.modules["requests"] = requests
        sys.modules["requests.exceptions"] = exceptions


_install_linux_stubs_if_needed()

from opencore_legacy_patcher import constants  # noqa: E402
from opencore_legacy_patcher.datasets import os_data  # noqa: E402
from opencore_legacy_patcher.detections import device_probe  # noqa: E402
from opencore_legacy_patcher.sys_patch import sys_patch, sys_patch_helpers  # noqa: E402
from opencore_legacy_patcher.sys_patch.auto_patcher.install import InstallAutomaticPatchingServices  # noqa: E402
from opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.auxiliary import AuxiliaryKernelCollection  # noqa: E402
from opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.support import KernelCacheSupport  # noqa: E402
from opencore_legacy_patcher.sys_patch.kernelcache.rebuild import RebuildKernelCache  # noqa: E402
from opencore_legacy_patcher.sys_patch.mount.mount import TargetVolumeGroup  # noqa: E402
from opencore_legacy_patcher.sys_patch.patchsets import (  # noqa: E402
    DynamicPatchset,
    HardwarePatchsetDetection,
    HardwarePatchsetSettings,
    HardwarePatchsetValidation,
    PatchType,
)


def _make_constants_for_host(model: str = "MacPro6,1", gpus=None) -> constants.Constants:
    c = constants.Constants()
    c.detected_os = os_data.os_data.sequoia.value
    c.detected_os_minor = 0
    c.detected_os_build = "24A000"
    c.detected_os_version = "15.0"
    c.computer = types.SimpleNamespace(
        real_model=model,
        reported_model=model,
        reported_board_id="Mac-F60DEB81FF30ACF6",
        rosetta_active=False,
        gpus=gpus or [],
        wifi=None,
        ethernet=[],
        usb_controllers=[],
        pcie_webcam=False,
        t1_chip=False,
        internal_keyboard_type="",
        trackpad_type="",
        backlight_type="",
        gmux_version=None,
        audio_type="",
        legacy_audio=False,
        modern_audio=False,
        webcam_type="",
    )
    return c


def _write_plist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(data, f)


class HardwarePatchsetDetectionOfflineTests(unittest.TestCase):
    def test_probes_target_os_and_resolves_offline_kdk_metallib_and_filevault(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root = Path(tmp) / "TargetSys"
            data_root = Path(tmp) / "TargetData"
            assets_dir = Path(tmp) / "OCPBP"
            assets_dir.mkdir(parents=True)
            (assets_dir / "KDK.pkg").write_bytes(b"kdk")
            (assets_dir / "MetallibSupportPkg.pkg").write_bytes(b"metallib")

            # Target is Sonoma 14.4 (23E214) even though Recovery constants say Sequoia 15.0 (24A000)
            _write_plist(
                sys_root / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "14.4", "ProductBuildVersion": "23E214"},
            )
            _write_plist(
                sys_root / "System/Library/Extensions/System.kext/Contents/Info.plist",
                {"CFBundleVersion": "23.4.0", "CFBundleShortVersionString": "23.4.0"},
            )

            gpu = device_probe.AMD(vendor_id=0x1002, device_id=0x6798, class_code=0x030000)
            c = _make_constants_for_host(gpus=[gpu])
            c.target_vol = str(sys_root)
            c.offline_assets_dir = assets_dir

            group = TargetVolumeGroup(
                container_disk="disk3",
                system_identifier="disk3s1",
                volume_name="Macintosh HD",
                data_identifier="disk3s5",
                data_volume_name="Macintosh HD - Data",
                system_mount=str(sys_root),
                data_mount=str(data_root),
                filevault_enabled=True,
                data_locked=False,
                is_live_booted=False,
            )

            # Without -allow_fv: FILEVAULT_ENABLED blocks patching against target volume group
            with mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", return_value=group), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.get_nvram", return_value=""), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.csr_decode", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.check_secure_boot_level", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.amfi_detect.AmfiConfigurationDetection.check_config", return_value=True), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.network_handler.NetworkUtilities.verify_network_connection", return_value=False):
                det_locked = HardwarePatchsetDetection(c)
                self.assertEqual(c.detected_os, os_data.os_data.sonoma.value)
                self.assertEqual(c.detected_os_build, "23E214")
                self.assertTrue(det_locked.device_properties[HardwarePatchsetValidation.FILEVAULT_ENABLED])
                self.assertFalse(det_locked.can_patch)

            # With -allow_fv and offline assets present: passes offline without network!
            def fake_nvram(var, uuid=None, decode=False):
                if var == "OCLP-Settings":
                    return "-allow_fv -allow_amfi"
                return ""

            with mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", return_value=group), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.get_nvram", side_effect=fake_nvram), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.csr_decode", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.check_secure_boot_level", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.amfi_detect.AmfiConfigurationDetection.check_config", return_value=True), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.network_handler.NetworkUtilities.verify_network_connection", return_value=False):
                det_ok = HardwarePatchsetDetection(c)
                self.assertTrue(det_ok.device_properties[HardwarePatchsetSettings.KERNEL_DEBUG_KIT_REQUIRED])
                self.assertFalse(det_ok.device_properties[HardwarePatchsetSettings.KERNEL_DEBUG_KIT_MISSING])
                self.assertFalse(det_ok.device_properties[HardwarePatchsetValidation.MISSING_NETWORK_CONNECTION])
                self.assertFalse(det_ok.device_properties[HardwarePatchsetValidation.FILEVAULT_ENABLED])
                self.assertTrue(det_ok.can_patch)

    def test_repatching_and_networking_checks_read_target_oclp_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root = Path(tmp) / "TargetSys"
            data_root = Path(tmp) / "TargetData"
            _write_plist(
                sys_root / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "15.2", "ProductBuildVersion": "24C101"},
            )
            _write_plist(
                sys_root / "System/Library/Extensions/System.kext/Contents/Info.plist",
                {"CFBundleVersion": "24.2.0"},
            )
            # Legacy Wireless v2.3.2 on target requires unpatching first (unpatch_required == "2.4.0")
            _write_plist(
                sys_root / "System/Library/CoreServices/OpenCore-Legacy-Patcher.plist",
                {"OpenCore Legacy Patcher": "2.3.2", "Legacy Wireless": {"Placeholder": True}},
            )

            wifi = device_probe.Broadcom(vendor_id=0x14E4, device_id=0x4331, class_code=0x028000)
            wifi.chipset = device_probe.Broadcom.Chipsets.AirPortBrcm4331
            c = _make_constants_for_host()
            c.computer.wifi = wifi

            with mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", side_effect=RuntimeError("no diskutil")), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.get_nvram", return_value="-allow_fv"), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.csr_decode", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.check_secure_boot_level", return_value=False), \
                 mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.amfi_detect.AmfiConfigurationDetection.check_config", return_value=True):
                det = HardwarePatchsetDetection(c, target_root=sys_root, target_data=data_root)
                self.assertTrue(det._already_has_networking_patches())
                self.assertTrue(det.device_properties[HardwarePatchsetValidation.REPATCHING_NOT_SUPPORTED])
                self.assertFalse(det.can_patch)


class KernelCacheTargetScopingTests(unittest.TestCase):
    def test_auxiliary_kc_uses_target_data_paths_and_skips_killall(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root = Path(tmp) / "TargetSys"
            data_root = Path(tmp) / "TargetData"
            policy_dir = data_root / "private/var/db/SystemPolicyConfiguration"
            policy_dir.mkdir(parents=True)
            (policy_dir / "KextPolicy").write_text("db")
            (policy_dir / "KextPolicy-wal").write_text("wal")

            cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmds.append([str(x) for x in cmd])
                if cmd[0] == "/bin/mkdir":
                    Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                elif cmd[0] == "/bin/rm":
                    Path(cmd[-1]).unlink(missing_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.auxiliary.subprocess_wrapper.run_as_root", side_effect=fake_rar):
                ok = RebuildKernelCache(
                    os_version=os_data.os_data.sequoia,
                    mount_location=str(sys_root),
                    auxiliary_cache=True,
                    auxiliary_cache_only=True,
                    mount_location_data=str(data_root),
                ).rebuild()

            self.assertTrue(ok)
            kmutil_cmd = next(c for c in cmds if c[0] == "/usr/bin/kmutil")
            self.assertIn("--aux-path", kmutil_cmd)
            self.assertIn(str(data_root / "Library/KernelCollections/AuxiliaryKernelExtensions.kc"), kmutil_cmd)
            self.assertIn("--repository", kmutil_cmd)
            self.assertIn(str(data_root / "Library/Extensions"), kmutil_cmd)
            self.assertFalse(any("killall" in c[0] for c in cmds))
            self.assertFalse((policy_dir / "KextPolicy").exists())
            self.assertFalse((policy_dir / "KextPolicy-wal").exists())

    def test_kernel_cache_support_fixes_path_join_and_targets_target_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_root = Path(tmp) / "TargetSys"
            data_root = Path(tmp) / "TargetData"
            src_root = Path(tmp) / "Source"

            # AuxKC instruction plist under target_data (not /)
            _write_plist(
                data_root / "private/var/db/KernelExtensionManagement/AuxKC/CurrentAuxKC/com.apple.kcgen.instructions.plist",
                {"kextsToBuild": {"k1": {"bundlePathMainOS": "/Library/Extensions/Approved.kext"}}},
            )
            _write_plist(
                src_root / "TestApple.kext/Contents/Info.plist",
                {"CFBundleIdentifier": "com.apple.driver.TestApple", "OSBundleRequired": "Root"},
            )
            _write_plist(
                sys_root / "System/Library/CoreServices/OpenCore-Legacy-Patcher.plist",
                {"PatchA": {PatchType.OVERWRITE_DATA_VOLUME: {"/Library/Extensions": {"OldPatch.kext": "1.0"}}}},
            )
            le_dir = data_root / "Library/Extensions"
            old_kext = le_dir / "OldPatch.kext"
            old_kext.mkdir(parents=True)
            ancient_kext = le_dir / "Ancient.kext"
            ancient_kext.mkdir(parents=True)
            # Set ancient timestamp (2020)
            os.utime(ancient_kext, (1580000000, 1580000000))

            support = KernelCacheSupport(
                mount_location_data=str(data_root),
                detected_os=os_data.os_data.sequoia.value,
                skip_root_kmutil_requirement=True,
                mount_location=str(sys_root),
            )
            self.assertFalse(support.check_kexts_needs_authentication("Approved.kext"))
            self.assertTrue(support.check_kexts_needs_authentication("Unapproved.kext"))

            dest = support.add_auxkc_support("TestApple.kext", str(src_root), "/System/Library/Extensions", f"{sys_root}/System/Library/Extensions")
            self.assertEqual(dest, f"{data_root}/Library/Extensions")
            with (src_root / "TestApple.kext/Contents/Info.plist").open("rb") as f:
                updated_info = plistlib.load(f)
            self.assertEqual(updated_info["OSBundleRequired"], "Auxiliary")

            cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmd_str = [str(x) for x in cmd]
                cmds.append(cmd_str)
                if cmd_str[0] == "/bin/rm":
                    shutil.rmtree(cmd_str[-1], ignore_errors=True)
                elif cmd_str[0] == "/bin/mkdir":
                    Path(cmd_str[-1]).mkdir(parents=True, exist_ok=True)
                elif cmd_str[0] == "/bin/mv":
                    shutil.move(cmd_str[-2], cmd_str[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.support.subprocess_wrapper.run_as_root", side_effect=fake_rar):
                support.clean_auxiliary_kc()

            self.assertFalse(old_kext.exists())
            self.assertFalse(ancient_kext.exists())
            self.assertTrue((data_root / "Library/Relocated Extensions/Ancient.kext").exists())


class SysPatchHelpersAndAutoPatcherTests(unittest.TestCase):
    def test_execute_rewriting_with_spaces_rsr_repair_windowserver_and_plist_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_mount = Path(tmp) / "Macintosh HD - Data"
            sys_mount = Path(tmp) / "Target Sys"
            (sys_mount / "private/etc").mkdir(parents=True)
            rsr_src = Path(tmp) / "RSRRepair"
            rsr_src.write_bytes(b"rsr")

            # WindowServer cache inside target_data
            ws_dir = data_mount / "private/var/folders/aa/bb/C/WindowServer/com.apple.WindowServer"
            ws_dir.mkdir(parents=True)
            (ws_dir / "cache.bin").write_bytes(b"1")

            c = _make_constants_for_host()
            c.detected_os = os_data.os_data.tahoe.value
            helpers = sys_patch_helpers.SysPatchHelpers(c)

            cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmds.append([str(x) for x in cmd])
                if cmd[0] == "/bin/rm":
                    shutil.rmtree(cmd[-1], ignore_errors=True)
                elif cmd[0] == "/bin/mkdir":
                    Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                elif cmd[0] == "/usr/bin/ditto":
                    shutil.copy2(cmd[-2], cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root", side_effect=fake_rar), \
                 mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar):
                # 1. Execute command rewriting with space in volume path
                cmd_str = "/usr/bin/defaults write /Library/Preferences/FeatureFlags/Domain/MessageProtection.plist KyberInTheSEPRegisteredKeys -dict Enabled -bool false"
                helpers.run_execute_command(cmd_str, as_root=True, target_data_mount=data_mount)
                defaults_cmd = next(x for x in cmds if os.path.basename(x[0]) == "defaults")
                self.assertEqual(defaults_cmd[2], str(data_mount / "Library/Preferences/FeatureFlags/Domain/MessageProtection.plist"))
                self.assertTrue((data_mount / "Library/Preferences/FeatureFlags/Domain").is_dir())

                # 2. RSRRepair offline install to <mount>/private/etc/rc.server
                with mock.patch.object(constants.Constants, "rsrrepair_userspace_path", new_callable=mock.PropertyMock, return_value=rsr_src):
                    helpers.install_rsr_repair_binary(mount_location=sys_mount, detected_os=os_data.os_data.tahoe.value)
                self.assertTrue((sys_mount / "private/etc/rc.server").is_file())
                self.assertFalse(any("--install" in x for x in cmds))

                # 3. WindowServer cleanup without chflags uchg
                helpers.disable_window_server_caching(target_data_mount=data_mount, detected_os=os_data.os_data.tahoe.value)
                self.assertFalse(ws_dir.exists())
                self.assertFalse(any("chflags" in " ".join(x) for x in cmds))

            # 4. Plist fallback when payload_path is not writable
            with mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.os.access", return_value=False):
                ok = helpers.generate_patchset_plist(
                    {"Test": {}},
                    "OpenCore-Legacy-Patcher.plist",
                    Path("/KDK"),
                    None,
                    detected_os=25,
                    detected_os_minor=0,
                    detected_os_build="25A100",
                )
            self.assertTrue(ok)
            self.assertIsNotNone(helpers.last_generated_plist_path)
            self.assertTrue(helpers.last_generated_plist_path.is_file())
            with helpers.last_generated_plist_path.open("rb") as f:
                pdata = plistlib.load(f)
            self.assertEqual(pdata["OS Version"], "25.0 (25A100)")
            shutil.rmtree(helpers.last_generated_plist_path.parent, ignore_errors=True)

    def test_auto_patcher_installs_app_helper_symlink_and_launch_services_to_target_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_mount = Path(tmp) / "TargetData"
            preboot_mount = Path(tmp) / "TargetPreboot"
            assets_dir = Path(tmp) / "OCPBP"
            assets_dir.mkdir(parents=True)
            autopkg = assets_dir / "AutoPkg-Assets.pkg"
            autopkg.write_bytes(b"pkg")

            # GPUCompanionBundles kext + cryptex OS.dmg on target
            le_kext = data_mount / "Library/Extensions/LegacyKext.kext/Contents"
            _write_plist(le_kext / "Info.plist", {"CFBundleIdentifier": "org.test.Legacy", "GPUCompanionBundles": ["a"]})
            (preboot_mount / "TARGET-UUID-999/cryptex1/current").mkdir(parents=True)
            (preboot_mount / "TARGET-UUID-999/cryptex1/current/OS.dmg").write_bytes(b"os")

            c = _make_constants_for_host()
            c.launcher_script = None
            template_snapshot = Path(c.rsr_monitor_launch_daemon_path).read_bytes()

            captured_cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmd_s = [str(x) for x in cmd]
                captured_cmds.append(cmd_s)
                prog = os.path.basename(cmd_s[0])
                if prog == "pkgutil" and cmd_s[1] == "--expand-full":
                    expanded = Path(cmd_s[3])
                    app_src = expanded / "Payload/Library/Application Support/Dortania/OpenCore-Patcher.app/Contents/MacOS"
                    app_src.mkdir(parents=True)
                    (app_src / "OpenCore-Patcher").write_bytes(b"bin")
                    helper_src = expanded / "Payload/Library/PrivilegedHelperTools/com.dortania.opencore-legacy-patcher.privileged-helper"
                    helper_src.parent.mkdir(parents=True)
                    helper_src.write_bytes(b"helper")
                elif prog == "mkdir":
                    Path(cmd_s[-1]).mkdir(parents=True, exist_ok=True)
                elif prog == "rm":
                    target = Path(cmd_s[-1])
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target, ignore_errors=True)
                elif prog == "mv":
                    Path(cmd_s[-1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(cmd_s[-2], cmd_s[-1])
                elif prog == "cp" or prog == "ditto":
                    src = Path(cmd_s[-2])
                    dst = Path(cmd_s[-1])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                elif prog == "ln" and "-s" in cmd_s:
                    os.symlink(cmd_s[-2], cmd_s[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root", side_effect=fake_rar), \
                 mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar):
                installer = InstallAutomaticPatchingServices(
                    c,
                    target_data_mount=data_mount,
                    preboot_mount=preboot_mount,
                    offline_assets_dir=assets_dir,
                )
                installer.install_auto_patcher_launch_agent(kdk_caching_needed=True)

            # OpenCore-Patcher.app, helper (+s), and /Applications symlink on target_data
            self.assertTrue((data_mount / "Library/Application Support/Dortania/OpenCore-Patcher.app/Contents/MacOS/OpenCore-Patcher").is_file())
            self.assertTrue((data_mount / "Library/PrivilegedHelperTools/com.dortania.opencore-legacy-patcher.privileged-helper").is_file())
            self.assertEqual(
                os.readlink(data_mount / "Applications/OpenCore-Patcher.app"),
                "/Library/Application Support/Dortania/OpenCore-Patcher.app",
            )
            self.assertFalse(any(os.path.basename(x[0]) == "installer" for x in captured_cmds))

            # LaunchAgents/Daemons on target_data
            self.assertTrue((data_mount / "Library/LaunchAgents/com.dortania.opencore-legacy-patcher.auto-patch.plist").is_file())
            self.assertTrue((data_mount / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.macos-update.plist").is_file())
            self.assertTrue((data_mount / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.os-caching.plist").is_file())
            rsr_mon = data_mount / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.rsr-monitor.plist"
            self.assertTrue(rsr_mon.is_file())
            with rsr_mon.open("rb") as f:
                mon_plist = plistlib.load(f)
            self.assertEqual(mon_plist["WatchPaths"], ["/System/Volumes/Preboot/TARGET-UUID-999/cryptex1/current/OS.dmg"])
            self.assertEqual(mon_plist["ProgramArguments"], ["/bin/rm", "-Rfv", "/Library/Extensions/LegacyKext.kext"])
            # Original repo template plist untouched!
            self.assertEqual(Path(c.rsr_monitor_launch_daemon_path).read_bytes(), template_snapshot)


class PatchSysVolumeEndToEndOfflineTests(unittest.TestCase):
    def test_start_patch_offline_end_to_end_deferred_probe_and_unpatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_sys = Path(tmp) / "stage_sys"
            data_root = Path(tmp) / "Target Data"
            preboot_root = Path(tmp) / "Target Preboot"
            assets_dir = Path(tmp) / "OCPBP"
            ub_dir = assets_dir / "Universal-Binaries"

            # Populate offline Universal-Binaries payloads needed by T1 Security Chip + Metal31001/Metallib
            (ub_dir / "13.6/usr/lib").mkdir(parents=True)
            (ub_dir / "13.6/usr/libexec").mkdir(parents=True)
            (ub_dir / "13.6/usr/standalone/firmware/nfrestore/firmware/fw").mkdir(parents=True)
            for d, names in {
                ub_dir / "13.6/usr/lib": ["libNFC_Comet.dylib", "libNFC_HAL.dylib", "libnfshared.dylib", "libnfshared.dylibOld.dylib", "libnfstorage.dylib", "libnfrestore.dylib", "libPN548_API.dylib"],
                ub_dir / "13.6/usr/libexec": ["biometrickitd", "nfcd", "nfrestore_service"],
                ub_dir / "13.6/usr/standalone/firmware/nfrestore/firmware/fw": ["PN549_FW_02_01_5A_rev88207.bin", "SN100V_FW_A3_01_01_81_rev127208.bin", "SN200V_FW_B1_02_01_86_rev127266.bin", "SN300V_FW_B0_02_01_22_rev129172.bin"],
            }.items():
                for n in names:
                    (d / n).write_bytes(b"bin")
            (ub_dir / "13.6/System/Library/PrivateFrameworks/EmbeddedOSInstall.framework").mkdir(parents=True)
            (ub_dir / "26.0-25G229/usr/libexec").mkdir(parents=True)
            (ub_dir / "26.0-25G229/usr/libexec/seld").write_bytes(b"seld")
            (ub_dir / "26.0-25G229/System/Library/PrivateFrameworks/LocalAuthenticationCore.framework").mkdir(parents=True)
            (ub_dir / "14.7.2-25/System/Library/PrivateFrameworks/NearField.framework").mkdir(parents=True)

            # Target System is initially unmounted; after RootVolumeMount.mount() it appears at stage_sys
            data_root.mkdir(parents=True)
            (data_root / "Library/Extensions").mkdir(parents=True)
            preboot_root.mkdir(parents=True)

            unmounted_group = TargetVolumeGroup(
                container_disk="disk3",
                system_identifier="disk3s1",
                volume_name="Macintosh HD",
                data_identifier="disk3s5",
                data_volume_name="Macintosh HD - Data",
                preboot_identifier="disk3s2",
                system_mount=None,
                data_mount=str(data_root),
                preboot_mount=str(preboot_root),
                filevault_enabled=True,
                data_locked=False,
                is_live_booted=False,
            )

            c = _make_constants_for_host(model="Macmini8,1")
            c.computer.t1_chip = True
            c.detected_os = os_data.os_data.sonoma.value  # Host Recovery differs from target Tahoe!
            c.detected_os_build = "23A000"
            c.detected_os_version = "14.0"
            c.target_vol = "disk3s1"
            c.offline_assets_dir = assets_dir
            c.wxpython_variant = False

            def fake_mount():
                # Materialize Tahoe 26.0 target system files when staging mount succeeds
                _write_plist(
                    stage_sys / "System/Library/CoreServices/SystemVersion.plist",
                    {"ProductVersion": "26.0", "ProductBuildVersion": "25A550"},
                )
                _write_plist(
                    stage_sys / "System/Library/Extensions/System.kext/Contents/Info.plist",
                    {"CFBundleVersion": "25.0.0"},
                )
                for p in (
                    "usr/lib",
                    "usr/libexec",
                    "usr/standalone/firmware/nfrestore/firmware/fw",
                    "System/Library/PrivateFrameworks",
                    "System/Library/Extensions",
                    "private/etc",
                ):
                    (stage_sys / p).mkdir(parents=True, exist_ok=True)
                psv.mount_obj.mount_path = str(stage_sys)
                psv.mount_obj.system_mount = str(stage_sys)
                psv.mount_obj.data_mount = str(data_root)
                psv.mount_obj.preboot_mount = str(preboot_root)
                return str(stage_sys)

            executed_cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmd_s = [str(x) for x in cmd]
                executed_cmds.append(cmd_s)
                prog = os.path.basename(cmd_s[0])
                if prog == "mkdir":
                    Path(cmd_s[-1]).mkdir(parents=True, exist_ok=True)
                elif prog == "rm":
                    target = Path(cmd_s[-1])
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target, ignore_errors=True)
                elif prog in ("cp", "ditto"):
                    src = Path(cmd_s[-2])
                    dst = Path(cmd_s[-1])
                    if src.is_dir():
                        shutil.copytree(src, dst / src.name if (dst.is_dir() and prog == "cp" and not str(cmd_s[-1]).endswith(src.name)) else dst, dirs_exist_ok=True)
                    else:
                        dst_file = (dst / src.name) if dst.is_dir() else dst
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst_file)
                elif prog == "rsync":
                    src = Path(cmd_s[-2].rstrip("/"))
                    dst = Path(cmd_s[-1].rstrip("/"))
                    shutil.copytree(src, dst / src.name, dirs_exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            def fake_nvram(var, uuid=None, decode=False):
                if var == "OCLP-Settings":
                    return "-allow_fv -allow_amfi"
                return ""

            patches = [
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.utilities.check_if_root_is_apfs_snapshot", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", return_value=unmounted_group),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.get_nvram", side_effect=fake_nvram),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.csr_decode", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.check_secure_boot_level", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.amfi_detect.AmfiConfigurationDetection.check_config", return_value=True),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"")),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.utilities.files.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.utilities.files.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.boot_system.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.auxiliary.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.support.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.mount.snapshot.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                psv = sys_patch.PatchSysVolume("Macmini8,1", c)
                with mock.patch.object(psv.mount_obj, "mount", side_effect=fake_mount), \
                     mock.patch.object(psv.mount_obj, "unmount", return_value=True) as mock_unmount:
                    psv.start_patch()

                    self.assertTrue(c.root_patcher_succeeded)
                    self.assertEqual(psv._target_os, os_data.os_data.tahoe.value)
                    self.assertTrue((stage_sys / "usr/libexec/seld").is_file())
                    self.assertTrue((stage_sys / "System/Library/CoreServices/OpenCore-Legacy-Patcher.plist").is_file())
                    self.assertTrue((data_root / "Library/LaunchAgents/com.dortania.opencore-legacy-patcher.auto-patch.plist").is_file())
                    # Verify PatchType.EXECUTE `defaults write` rewrote `/Library/...` onto `data_root`
                    defaults_cmds = [x for x in executed_cmds if os.path.basename(x[0]) == "defaults"]
                    self.assertTrue(defaults_cmds)
                    self.assertEqual(defaults_cmds[0][2], str(data_root / "Library/Preferences/FeatureFlags/Domain/MessageProtection.plist"))
                    # Verify kmutil --new aux target-scoped to stage_sys and data_root
                    kmutil_cmds = [x for x in executed_cmds if x[0] == "/usr/bin/kmutil"]
                    self.assertTrue(kmutil_cmds)
                    self.assertIn(str(stage_sys / "System/Library/KernelCollections/BootKernelExtensions.kc"), kmutil_cmds[0])
                    self.assertIn(str(data_root / "Library/KernelCollections/AuxiliaryKernelExtensions.kc"), kmutil_cmds[0])
                    # Verify unmounted cleanly and dmg override cleaned up
                    mock_unmount.assert_called()
                    self.assertIsNone(c._payload_local_binaries_root_path_override)

                # Now test offline unpatch against the same target!
                c.root_patcher_succeeded = False
                skylight_dir = data_root / "Library/Application Support/SkyLightPlugins"
                skylight_dir.mkdir(parents=True)
                psv_unpatch = sys_patch.PatchSysVolume("Macmini8,1", c)
                with mock.patch.object(psv_unpatch.mount_obj, "mount", side_effect=lambda: (
                    setattr(psv_unpatch.mount_obj, "mount_path", str(stage_sys)) or str(stage_sys)
                )), mock.patch.object(psv_unpatch.mount_obj, "unmount", return_value=True) as mock_unpatch_unmount:
                    psv_unpatch.start_unpatch()

                self.assertTrue(c.root_patcher_succeeded)
                self.assertFalse(skylight_dir.exists())
                self.assertTrue(any("--last-sealed-snapshot" in x and str(stage_sys) in x for x in executed_cmds))
                mock_unpatch_unmount.assert_called()

    def test_start_patch_offline_with_kdk_metallib_rsrrepair_and_windowserver(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_sys = Path(tmp) / "stage_sys"
            data_root = Path(tmp) / "Target Data"
            preboot_root = Path(tmp) / "Target Preboot"
            assets_dir = Path(tmp) / "OCPBP"
            ub_dir = assets_dir / "Universal-Binaries"
            ub_dir.mkdir(parents=True)
            data_root.mkdir(parents=True)
            preboot_root.mkdir(parents=True)

            (assets_dir / "KDK.pkg").write_bytes(b"kdk")
            (assets_dir / "MetallibSupportPkg.pkg").write_bytes(b"metallib")
            (assets_dir / "AutoPkg-Assets.pkg").write_bytes(b"autopkg")
            rsr_src = Path(tmp) / "RSRRepair"
            rsr_src.write_bytes(b"rsr")

            ws_cache = data_root / "private/var/folders/11/22/C/WindowServer/com.apple.WindowServer"
            ws_cache.mkdir(parents=True)
            (ws_cache / "shader.cache").write_bytes(b"stale")

            _write_plist(
                stage_sys / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "15.2", "ProductBuildVersion": "24C101"},
            )
            _write_plist(
                stage_sys / "System/Library/Extensions/System.kext/Contents/Info.plist",
                {"CFBundleVersion": "24.2.0"},
            )
            (stage_sys / "System/Library/Extensions").mkdir(parents=True, exist_ok=True)
            (stage_sys / "System/Library/Frameworks/CoreDisplay.framework").mkdir(parents=True, exist_ok=True)
            (stage_sys / "private/etc").mkdir(parents=True, exist_ok=True)

            group = TargetVolumeGroup(
                container_disk="disk3",
                system_identifier="disk3s1",
                volume_name="Macintosh HD",
                data_identifier="disk3s5",
                data_volume_name="Macintosh HD - Data",
                preboot_identifier="disk3s2",
                system_mount=str(stage_sys),
                data_mount=str(data_root),
                preboot_mount=str(preboot_root),
                filevault_enabled=False,
                data_locked=False,
                is_live_booted=False,
            )

            gpu = device_probe.AMD(vendor_id=0x1002, device_id=0x6798, class_code=0x030000)
            c = _make_constants_for_host(model="MacPro6,1", gpus=[gpu])
            c.target_vol = str(stage_sys)
            c.offline_assets_dir = assets_dir
            c.wxpython_variant = False

            executed_cmds = []

            def fake_rar(cmd, *args, **kwargs):
                cmd_s = [str(x) for x in cmd]
                executed_cmds.append(cmd_s)
                prog = os.path.basename(cmd_s[0])
                if prog == "pkgutil" and cmd_s[1] == "--expand-full":
                    pkg_src = Path(cmd_s[2]).name
                    expanded = Path(cmd_s[3])
                    if "KDK" in pkg_src:
                        kdk_root = expanded / "Payload/Library/Developer/KDKs/KDK_15.2_24C101.kdk"
                        (kdk_root / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext").mkdir(parents=True)
                        (kdk_root / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern").write_bytes(b"k")
                        _write_plist(
                            kdk_root / "System/Library/CoreServices/SystemVersion.plist",
                            {"ProductVersion": "15.2", "ProductBuildVersion": "24C101"},
                        )
                    elif "Metallib" in pkg_src:
                        ml_root = expanded / "Payload/Library/Application Support/Dortania/MetallibSupportPkg/15.2-24C101"
                        (ml_root / "System/Library/Frameworks/CoreDisplay.framework").mkdir(parents=True)
                        (ml_root / "System/Library/Frameworks/CoreDisplay.framework/default.metallib").write_bytes(b"ml")
                    elif "AutoPkg" in pkg_src:
                        app_dir = expanded / "Payload/Library/Application Support/Dortania/OpenCore-Patcher.app/Contents/MacOS"
                        app_dir.mkdir(parents=True)
                        (app_dir / "OpenCore-Patcher").write_bytes(b"app")
                elif prog == "mkdir":
                    Path(cmd_s[-1]).mkdir(parents=True, exist_ok=True)
                elif prog == "rm":
                    target = Path(cmd_s[-1])
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target, ignore_errors=True)
                elif prog == "mv":
                    Path(cmd_s[-1]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(cmd_s[-2], cmd_s[-1])
                elif prog in ("cp", "ditto"):
                    src = Path(cmd_s[-2])
                    dst = Path(cmd_s[-1])
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        dst_file = (dst / src.name) if dst.is_dir() else dst
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst_file)
                elif prog == "rsync":
                    src = Path(cmd_s[-2].rstrip("/"))
                    dst = Path(cmd_s[-1].rstrip("/"))
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                elif prog == "ln" and "-s" in cmd_s:
                    os.symlink(cmd_s[-2], cmd_s[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            patches = [
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.utilities.check_if_root_is_apfs_snapshot", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", return_value=group),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.get_nvram", return_value="-allow_fv -allow_amfi"),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.csr_decode", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.utilities.check_secure_boot_level", return_value=False),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.amfi_detect.AmfiConfigurationDetection.check_config", return_value=True),
                mock.patch("opencore_legacy_patcher.support.kdk_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False),
                mock.patch("opencore_legacy_patcher.support.metallib_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False),
                mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"")),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"")),
                mock.patch("opencore_legacy_patcher.sys_patch.patchsets.detect.network_handler.NetworkUtilities.verify_network_connection", return_value=False),
                mock.patch.object(constants.Constants, "rsrrepair_userspace_path", new_callable=mock.PropertyMock, return_value=rsr_src),
                mock.patch("opencore_legacy_patcher.support.kdk_handler.KernelDebugKitObject._local_kdk_valid_legacy", return_value=True),
                mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.support.metallib_handler.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.utilities.kdk_merge.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.utilities.files.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.utilities.files.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch_helpers.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.sys_patch.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.boot_system.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.kernelcache.kernel_collection.support.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.mount.snapshot.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root", side_effect=fake_rar),
                mock.patch("opencore_legacy_patcher.sys_patch.auto_patcher.install.subprocess_wrapper.run_as_root_and_verify", side_effect=fake_rar),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                psv = sys_patch.PatchSysVolume("MacPro6,1", c)
                psv.patch_set_dictionary = {
                    "AMD Legacy GCN": {
                        PatchType.OVERWRITE_SYSTEM_VOLUME: {
                            "/System/Library/Frameworks/CoreDisplay.framework": {
                                "default.metallib": DynamicPatchset.MetallibSupportPkg,
                            },
                        },
                    },
                }
                psv.requires_metallib_caching = True
                psv._patch_root_vol()

            self.assertTrue(c.root_patcher_succeeded)
            # KDK and Metallib extracted onto target_data
            self.assertTrue((data_root / "Library/Developer/KDKs/KDK_15.2_24C101.kdk").is_dir())
            self.assertTrue((data_root / "Library/Application Support/Dortania/MetallibSupportPkg/15.2-24C101").is_dir())
            self.assertTrue((stage_sys / "System/Library/Frameworks/CoreDisplay.framework/default.metallib").is_file())
            # WindowServer cache cleaned on target_data, chflags deferred (never invoked)
            self.assertFalse(ws_cache.exists())
            self.assertFalse(any("chflags" in " ".join(x) for x in executed_cmds))
            # Boot & System KC rebuilt against --volume-root stage_sys, and RSRRepair staged to private/etc/rc.server
            kmutil_cmds = [x for x in executed_cmds if x[0] == "/usr/bin/kmutil"]
            self.assertTrue(kmutil_cmds)
            self.assertIn("--volume-root", kmutil_cmds[0])
            self.assertIn(str(stage_sys), kmutil_cmds[0])
            self.assertTrue((stage_sys / "private/etc/rc.server").is_file())
            # OpenCore-Patcher.app, os-caching daemon, and LaunchAgent installed onto target_data
            self.assertTrue((data_root / "Library/Application Support/Dortania/OpenCore-Patcher.app/Contents/MacOS/OpenCore-Patcher").is_file())
            self.assertTrue((data_root / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.os-caching.plist").is_file())
            self.assertTrue((data_root / "Library/LaunchAgents/com.dortania.opencore-legacy-patcher.auto-patch.plist").is_file())


if __name__ == "__main__":
    unittest.main()

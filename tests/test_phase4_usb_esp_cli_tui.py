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
                return types.SimpleNamespace(value=0x803, can_edit_root=True, can_load_arbitrary_kexts=True)

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

from opencore_legacy_patcher import application_entry, constants
from opencore_legacy_patcher.ocpbp import esp_sync, tui
from opencore_legacy_patcher.support import arguments, macos_installer_handler, reroute_payloads, utilities
from opencore_legacy_patcher.sys_patch.mount import mount


class Phase4CLITests(unittest.TestCase):
    def test_check_cli_args_parses_ocpbp_flags(self):
        argv = [
            "ocpbp",
            "--target_vol", "/Volumes/Macintosh HD",
            "--offline_assets_dir", "/Volumes/OCPBP",
            "--passphrase", "secret",
            "--sync_esp",
            "--esp_disk", "disk0s1",
            "--model", "MacPro6,1",
        ]
        with mock.patch.object(sys, "argv", argv):
            parsed = utilities.check_cli_args()

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.target_vol, "/Volumes/Macintosh HD")
        self.assertEqual(parsed.offline_assets_dir, "/Volumes/OCPBP")
        self.assertEqual(parsed.passphrase, "secret")
        self.assertTrue(parsed.sync_esp)
        self.assertEqual(parsed.esp_disk, "disk0s1")
        self.assertEqual(parsed.model, "MacPro6,1")

    def test_discover_offline_assets_defaults_to_ocpbp_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            ocpbp_mount = Path(tmp) / "OCPBP"
            ocpbp_mount.mkdir()
            gconst = constants.Constants()
            gconst.ocpbp_volume_mount = ocpbp_mount
            gconst.launcher_binary = None

            found = esp_sync.discover_offline_assets_dir(None, gconst)
            self.assertEqual(found, ocpbp_mount)

            with self.assertRaises(FileNotFoundError):
                esp_sync.discover_offline_assets_dir(Path(tmp) / "missing", gconst)

    def test_arguments_target_vol_and_sync_esp_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            assets_dir.mkdir()
            gconst = constants.Constants()
            gconst.computer = types.SimpleNamespace(real_model="MacPro6,1")
            gconst.ocpbp_volume_mount = assets_dir

            patcher_instance = mock.Mock()
            patcher_instance.patch_set_dictionary = {"Graphics": {}}

            def _fake_start_patch():
                gconst.root_patcher_succeeded = True

            patcher_instance.start_patch.side_effect = _fake_start_patch

            sync_manager_instance = mock.Mock()
            sync_manager_instance.sync_internal_esp.return_value = {"mode": "rebuild", "esp_partition": "disk0s1"}

            argv = ["ocpbp", "--target_vol", "/Volumes/Macintosh HD", "--sync_esp", "--passphrase", "pw"]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("opencore_legacy_patcher.support.arguments.sys_patch.PatchSysVolume", return_value=patcher_instance) as patch_cls,
                mock.patch("opencore_legacy_patcher.ocpbp.esp_sync.ESPSyncManager", return_value=sync_manager_instance),
                mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.APFSVolumeResolver.resolve_target", return_value=None),
            ):
                arguments.arguments(gconst)

            patch_cls.assert_called_once()
            self.assertEqual(patch_cls.call_args.kwargs["target_vol"], "/Volumes/Macintosh HD")
            self.assertEqual(patch_cls.call_args.kwargs["offline_assets_dir"], assets_dir)
            self.assertEqual(patch_cls.call_args.kwargs["passphrase"], "pw")
            sync_manager_instance.sync_internal_esp.assert_called_once()

    def test_headless_entry_never_imports_wx_gui(self):
        fake_computer = types.SimpleNamespace(
            real_model="MacPro6,1",
            opencore_path=None,
            firmware_vendor="Apple",
            gpus=[],
            wifi=None,
            oclp_sys_signed=False,
        )
        fake_os = mock.Mock()
        fake_os.detect_kernel_major.return_value = 24
        fake_os.detect_kernel_minor.return_value = 0
        fake_os.detect_os_build.return_value = "24A335"
        fake_os.detect_os_version.return_value = "15.0"

        with (
            mock.patch.object(sys, "argv", ["ocpbp"]),
            mock.patch("opencore_legacy_patcher.detections.os_probe.OSProbe", return_value=fake_os),
            mock.patch("opencore_legacy_patcher.detections.device_probe.Computer.probe", return_value=fake_computer),
            mock.patch("opencore_legacy_patcher.support.utilities.find_disk_off_uuid", return_value=None),
            mock.patch("opencore_legacy_patcher.support.utilities.check_recovery", return_value=True),
            mock.patch("opencore_legacy_patcher.support.defaults.GenerateDefaults"),
            mock.patch("opencore_legacy_patcher.ocpbp.tui.OCPBPTerminalUI.run", return_value=0) as tui_run,
            mock.patch.dict(sys.modules, {"opencore_legacy_patcher.wx_gui": None, "wx": None}),
        ):
            app = application_entry.OpenCoreLegacyPatcher(headless=True)

        self.assertFalse(app.constants.wxpython_variant)
        self.assertTrue(app.constants.ocpbp_cli_mode)
        tui_run.assert_called_once()

    def test_reroute_payloads_uses_bundled_payloads_without_hdiutil_in_compiled_ocpbp(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_root = Path(tmp)
            (bundle_root / "payloads").mkdir()
            gconst = constants.Constants()
            gconst.wxpython_variant = False
            gconst.launcher_script = None
            gconst.original_path = bundle_root
            gconst.current_path = bundle_root

            with mock.patch("subprocess.run") as subproc:
                router = reroute_payloads.RoutePayloadDiskImage(gconst)

            subproc.assert_not_called()
            self.assertEqual(gconst.payload_path, bundle_root / "payloads")
            self.assertNotEqual(gconst.current_path, bundle_root)
            self.assertTrue(Path(router.temp_dir.name).exists())


class Phase4ESPSyncTests(unittest.TestCase):
    def test_enforce_efi_config_flags_and_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist_path = Path(tmp) / "config.plist"
            initial = {
                "NVRAM": {
                    "Add": {
                        esp_sync.APPLE_NVRAM_GUID: {
                            "csr-active-config": b"\x00\x00\x00\x00",
                            "boot-args": "-v",
                        },
                        esp_sync.OCLP_NVRAM_GUID: {"OCLP-Settings": "-dummy"},
                    },
                    "Delete": {
                        esp_sync.APPLE_NVRAM_GUID: [],
                        esp_sync.OCLP_NVRAM_GUID: [],
                    },
                },
                "Misc": {"Security": {"SecureBootModel": "j137ap"}},
                "Kernel": {"Patch": [{"Comment": "Force FileVault on Broken Seal", "Enabled": False}]},
            }
            with plist_path.open("wb") as handle:
                plistlib.dump(initial, handle)

            before = esp_sync.inspect_efi_config(plist_path)
            self.assertFalse(before["ready_for_root_patch"])
            self.assertFalse(before["sip_allows_root_patch"])
            self.assertFalse(before["secure_boot_disabled"])

            after = esp_sync.enforce_efi_config_flags(plist_path)
            self.assertTrue(after["ready_for_root_patch"])
            self.assertTrue(after["sip_allows_root_patch"])
            self.assertTrue(after["secure_boot_disabled"])
            self.assertTrue(after["allow_fv_enabled"])
            self.assertTrue(after["nvram_deletes_csr"])

            with plist_path.open("rb") as handle:
                saved = plistlib.load(handle)
            self.assertEqual(saved["Misc"]["Security"]["SecureBootModel"], "Disabled")
            self.assertTrue(saved["Kernel"]["Patch"][0]["Enabled"])

    def test_discover_esp_targets_prioritizes_target_internal_disk(self):
        gconst = constants.Constants()
        gconst.booted_oc_disk = "disk4s1"

        fake_disks = {
            "disk4": {
                "disk": "disk4",
                "name": "USB Flash Drive",
                "size": "32.0 GB",
                "partitions": {
                    "disk4s1": {"fs": "msdos", "name": "EFI", "size": 209715200},
                },
            },
            "disk0": {
                "disk": "disk0",
                "name": "APPLE SSD SM0512",
                "size": "500.0 GB",
                "partitions": {
                    "disk0s1": {"fs": "msdos", "name": "EFI", "size": 209715200},
                },
            },
        }

        target_group = mount.TargetVolumeGroup(
            container_disk="disk1",
            system_identifier="disk1s5",
            volume_name="Macintosh HD",
            physical_stores=["disk0s2"],
        )

        installer_mock = mock.Mock()
        installer_mock.list_disks.return_value = fake_disks
        installer_mock.list_partitions.side_effect = lambda d, ds: {
            p: {"partition": p, "name": v["name"], "size": "209.7 MB"}
            for p, v in ds[d]["partitions"].items()
        }

        def _fake_diskutil_info(ident):
            if ident == "disk0s2":
                return {"ParentWholeDisk": "disk0"}
            if ident == "disk0":
                return {"Internal": True, "BusProtocol": "PCI-Express"}
            if ident == "disk4":
                return {"Internal": False, "BusProtocol": "USB"}
            return {}

        with (
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync.install.disk_installation", return_value=installer_mock),
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync._diskutil_info", side_effect=_fake_diskutil_info),
        ):
            candidates = esp_sync.ESPSyncManager(gconst).discover_esp_targets(target_group=target_group)

        self.assertEqual([c["partition"] for c in candidates], ["disk0s1", "disk4s1"])
        self.assertTrue(candidates[0]["is_target_disk"])
        self.assertTrue(candidates[0]["is_internal"])
        self.assertTrue(candidates[1]["is_booted_oc"])

    def test_sync_internal_esp_rebuild_enforces_flags_and_installs(self):
        gconst = constants.Constants()
        gconst.computer = types.SimpleNamespace(real_model="MacPro6,1", gpus=[], wifi=None, oclp_sys_signed=False)
        gconst.booted_oc_disk = "disk4s1"

        installed_target = []

        def _fake_build(model, bconst):
            Path(bconst.oc_folder).mkdir(parents=True, exist_ok=True)
            plist = {
                "NVRAM": {
                    "Add": {
                        esp_sync.APPLE_NVRAM_GUID: {"csr-active-config": b"\x03\x08\x00\x00", "boot-args": ""},
                        esp_sync.OCLP_NVRAM_GUID: {"OCLP-Settings": "-allow_fv"},
                    },
                    "Delete": {esp_sync.APPLE_NVRAM_GUID: ["csr-active-config"], esp_sync.OCLP_NVRAM_GUID: []},
                },
                "Misc": {"Security": {"SecureBootModel": "Disabled"}},
                "Kernel": {"Patch": []},
            }
            with Path(bconst.plist_path).open("wb") as handle:
                plistlib.dump(plist, handle)
            self.assertFalse(bconst.sip_status)
            self.assertFalse(bconst.secure_status)

        installer_mock = mock.Mock()
        installer_mock.list_disks.return_value = {
            "disk0": {"disk": "disk0", "name": "Internal SSD", "size": "500 GB", "partitions": {"disk0s1": {"fs": "msdos", "name": "EFI", "size": 209715200}}}
        }
        installer_mock.list_partitions.return_value = {
            "disk0s1": {"partition": "disk0s1", "name": "EFI", "size": "209.7 MB"}
        }

        def _install_oc(part):
            installed_target.append(part)
            return True

        installer_mock.install_opencore.side_effect = _install_oc

        with (
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync.install.disk_installation", return_value=installer_mock),
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync._diskutil_info", return_value={"Internal": True, "BusProtocol": "PCI-Express"}),
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync.defaults.GenerateDefaults"),
            mock.patch("opencore_legacy_patcher.ocpbp.esp_sync.build.BuildOpenCore", side_effect=_fake_build),
        ):
            res = esp_sync.ESPSyncManager(gconst).sync_internal_esp(model="MacPro6,1", mode="rebuild")

        self.assertEqual(res["mode"], "rebuild")
        self.assertEqual(res["esp_partition"], "disk0s1")
        self.assertTrue(res["inspection"]["ready_for_root_patch"])
        self.assertEqual(installed_target, ["disk0s1"])

    def test_sync_internal_esp_clone_copies_and_enforces_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_mount = Path(tmp) / "src_efi"
            dst_mount = Path(tmp) / "dst_efi"
            (src_mount / "EFI/OC").mkdir(parents=True)
            (src_mount / "EFI/BOOT").mkdir(parents=True)
            (src_mount / "System/Library/CoreServices").mkdir(parents=True)
            (src_mount / "System/Library/CoreServices/boot.efi").write_bytes(b"boot")
            (src_mount / "EFI/BOOT/BOOTx64.efi").write_bytes(b"oc")
            dst_mount.mkdir(parents=True)

            src_plist = {
                "NVRAM": {
                    "Add": {
                        esp_sync.APPLE_NVRAM_GUID: {"csr-active-config": b"\x00\x00\x00\x00"},
                        esp_sync.OCLP_NVRAM_GUID: {},
                    },
                    "Delete": {esp_sync.APPLE_NVRAM_GUID: []},
                },
                "Misc": {"Security": {"SecureBootModel": "j160ap"}},
                "Kernel": {"Patch": []},
            }
            with (src_mount / "EFI/OC/config.plist").open("wb") as handle:
                plistlib.dump(src_plist, handle)

            gconst = constants.Constants()
            gconst.booted_oc_disk = "disk4s1"

            def _ensure_mounted(part):
                return (src_mount, False) if part == "disk4s1" else (dst_mount, False)

            with (
                mock.patch.object(esp_sync.ESPSyncManager, "discover_esp_targets", return_value=[{"partition": "disk0s1", "is_target_disk": True, "is_internal": True, "is_booted_oc": False}]),
                mock.patch("opencore_legacy_patcher.ocpbp.esp_sync._ensure_partition_mounted", side_effect=_ensure_mounted),
            ):
                res = esp_sync.ESPSyncManager(gconst).sync_internal_esp(esp_partition="disk0s1", mode="clone")

            self.assertEqual(res["mode"], "clone")
            self.assertTrue((dst_mount / "EFI/OC/config.plist").is_file())
            self.assertTrue(res["inspection"]["ready_for_root_patch"])


class Phase4TUITests(unittest.TestCase):
    def test_tui_unlocks_patches_and_syncs_esp(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            assets_dir.mkdir()
            (assets_dir / "Universal-Binaries.dmg").write_bytes(b"dmg")

            gconst = constants.Constants()
            gconst.computer = types.SimpleNamespace(real_model="MacPro6,1")
            gconst.recovery_status = True
            gconst.offline_assets_dir = assets_dir

            locked_group = mount.TargetVolumeGroup(
                container_disk="disk1",
                system_identifier="disk1s5",
                volume_name="Macintosh HD",
                data_identifier="disk1s1",
                data_volume_name="Macintosh HD - Data",
                system_mount="/Volumes/Macintosh HD",
                filevault_enabled=True,
                data_locked=True,
                physical_stores=["disk0s2"],
            )

            resolver_mock = mock.Mock()
            resolver_mock.discover_volume_groups.return_value = [locked_group]

            def _fake_unlock(grp, passphrase=None, interactive=False):
                self.assertEqual(passphrase, "fv-pass")
                grp.data_locked = False
                grp.data_mount = "/Volumes/Macintosh HD - Data"
                return grp

            resolver_mock.unlock_data_volume.side_effect = _fake_unlock

            patcher_mock = mock.Mock()
            patcher_mock.start_patch.side_effect = lambda: setattr(gconst, "root_patcher_succeeded", True)

            esp_mock = mock.Mock()
            esp_mock.discover_esp_targets.return_value = [
                {"partition": "disk0s1", "disk": "disk0", "disk_name": "Internal SSD", "size": "209 MB", "is_target_disk": True, "is_internal": True, "is_booted_oc": False}
            ]
            esp_mock.sync_internal_esp.return_value = {
                "mode": "rebuild",
                "esp_partition": "disk0s1",
                "inspection": {"csr_active_config_hex": "0x00000803", "secure_boot_model": "Disabled", "allow_fv_enabled": True},
            }

            outputs = []
            # Menu: 2 (Patch) -> 2-confirm security n/a since stub CSR has 0x803 -> prompt y for ESP sync -> pick 1 -> mode 1 -> 0 (Exit)
            answers = iter(["2", "y", "1", "1", "0"])

            with (
                mock.patch("opencore_legacy_patcher.ocpbp.tui.mount.APFSVolumeResolver", return_value=resolver_mock),
                mock.patch("opencore_legacy_patcher.ocpbp.tui.esp_sync.ESPSyncManager", return_value=esp_mock),
                mock.patch("opencore_legacy_patcher.ocpbp.tui.esp_sync.get_live_boot_security_status", return_value={"csr_active_config_hex": "0x00000803", "sip_allows_root_patch": True, "secure_boot_active": False, "ready_for_root_patch": True}),
                mock.patch("opencore_legacy_patcher.ocpbp.tui.sys_patch.PatchSysVolume", return_value=patcher_mock) as patch_cls,
            ):
                ui = tui.OCPBPTerminalUI(
                    gconst,
                    input_fn=lambda _prompt: next(answers),
                    output_fn=outputs.append,
                    passphrase_fn=lambda _prompt: "fv-pass",
                )
                rc = ui.run()

            self.assertEqual(rc, 0)
            resolver_mock.unlock_data_volume.assert_called_once()
            patch_cls.assert_called_once()
            self.assertEqual(patch_cls.call_args.kwargs["target_vol"], "/Volumes/Macintosh HD")
            esp_mock.sync_internal_esp.assert_called_once()


class Phase4USBLayoutTests(unittest.TestCase):
    def test_compute_ocpbp_usb_layout_sizing_and_small_disk_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Install macOS Tahoe.app"
            (app / "Contents/SharedSupport").mkdir(parents=True)
            # Small mock payload; layout still enforces 17 GB minimum OCLP-Installer partition
            (app / "Contents/SharedSupport/SharedSupport.dmg").write_bytes(b"0" * 1024)

            small_16gb = macos_installer_handler.compute_ocpbp_usb_layout(15_800_000_000, app)
            self.assertFalse(small_16gb["fits"])
            self.assertIn("Needs at least", small_16gb["reason"])

            large_32gb = macos_installer_handler.compute_ocpbp_usb_layout(32_000_000_000, app)
            self.assertTrue(large_32gb["fits"])
            self.assertEqual(large_32gb["installer_size_arg"], "17G")
            self.assertGreaterEqual(large_32gb["ocpbp_estimated_bytes"], 4_000_000_000)

    def test_generate_installer_creation_script_3partition_vs_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            ia_src = tmp_root / "Install macOS Sequoia.app"
            (ia_src / "Contents/Resources").mkdir(parents=True)
            cim = ia_src / "Contents/Resources/createinstallmedia"
            cim.write_text("#!/bin/sh\necho Install media now available at\n")
            with (ia_src / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump({"DTPlatformVersion": "15.0"}, handle)

            work_tmp = tmp_root / "work_tmp"
            work_tmp.mkdir()
            script_out = tmp_root / "payloads"
            script_out.mkdir()

            def _fake_copy(src, dst):
                dst_path = Path(dst) / Path(src).name if Path(dst).is_dir() else Path(dst)
                if dst_path.exists():
                    shutil.rmtree(dst_path)
                shutil.copytree(src, dst_path)

            creator = macos_installer_handler.InstallerCreation()
            with (
                mock.patch.object(macos_installer_handler, "tmp_dir", types.SimpleNamespace(name=str(work_tmp))),
                mock.patch("opencore_legacy_patcher.support.macos_installer_handler.can_copy_on_write", return_value=True),
                mock.patch("opencore_legacy_patcher.support.macos_installer_handler.subprocess.run", side_effect=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=b"") if "codesign" in cmd[0] else _fake_copy(cmd[2], cmd[3]) if len(cmd) >= 4 else types.SimpleNamespace(returncode=0, stdout=b"")),
            ):
                ok_ocpbp = creator.generate_installer_creation_script(
                    str(script_out),
                    str(ia_src),
                    "/dev/disk4",
                    ocpbp_enabled=True,
                    installer_partition_size="19G",
                )
                self.assertTrue(ok_ocpbp)
                ocpbp_script = (script_out / "Installer.sh").read_text()
                self.assertIn("diskutil partitionDisk /dev/disk4 GPT JHFS+ OCLP-Installer 19G JHFS+ OCPBP R", ocpbp_script)
                self.assertNotIn("eraseDisk", ocpbp_script)
                self.assertIn("--volume /Volumes/OCLP-Installer --nointeraction", ocpbp_script)

                ok_legacy = creator.generate_installer_creation_script(
                    str(script_out),
                    str(ia_src),
                    "/dev/disk4",
                    ocpbp_enabled=False,
                )
                self.assertTrue(ok_legacy)
                legacy_script = (script_out / "Installer.sh").read_text()
                self.assertIn("diskutil eraseDisk HFS+ OCLP-Installer /dev/disk4", legacy_script)

    def test_populate_ocpbp_volume_stages_assets_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payloads_dir = root / "payloads"
            payloads_dir.mkdir()
            autopkg = payloads_dir / "AutoPkg-Assets.pkg"
            autopkg.write_bytes(b"pkg")

            ub = root / "Universal-Binaries.dmg"
            ub.write_bytes(b"dmg")

            bin_dir = root / "dist"
            bin_dir.mkdir()
            ocpbp_bin = bin_dir / "ocpbp"
            ocpbp_bin.write_bytes(b"#!/bin/sh\necho ocpbp\n")

            ocpbp_mount = root / "Volumes_OCPBP"

            gconst = constants.Constants()
            gconst.original_path = root
            gconst.payload_path = payloads_dir
            gconst.launcher_binary = str(bin_dir / "OpenCore-Patcher")

            def _cp(cmd, **kw):
                shutil.copy2(cmd[-2], cmd[-1])
                return types.SimpleNamespace(returncode=0)

            with (
                mock.patch("opencore_legacy_patcher.support.macos_installer_handler.generate_copy_arguments", side_effect=lambda s, d: ["cp", s, d]),
                mock.patch("opencore_legacy_patcher.support.macos_installer_handler.subprocess.run", side_effect=_cp),
            ):
                res = macos_installer_handler.InstallerCreation().populate_ocpbp_volume(
                    gconst,
                    ocpbp_mount,
                    installer_os_version="26.0",
                    installer_build="25A5279m",
                    target_model="MacPro6,1",
                    download_if_missing=False,
                )

            self.assertTrue((ocpbp_mount / "ocpbp").is_file())
            self.assertTrue((ocpbp_mount / "ocpbp.command").is_file())
            self.assertTrue(os.access(ocpbp_mount / "ocpbp.command", os.X_OK))
            self.assertTrue((ocpbp_mount / "AutoPkg-Assets.pkg").is_file())
            self.assertTrue((ocpbp_mount / "Universal-Binaries.dmg").is_file())
            self.assertIsNotNone(res["manifest"])
            with (ocpbp_mount / ".ocpbp-manifest.plist").open("rb") as handle:
                manifest = plistlib.load(handle)
            self.assertEqual(manifest["InstallerOSVersion"], "26.0")
            self.assertEqual(manifest["TargetModel"], "MacPro6,1")


if __name__ == "__main__":
    unittest.main()

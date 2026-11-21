import importlib
import io
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
from opencore_legacy_patcher.detections import os_probe  # noqa: E402
from opencore_legacy_patcher.support import kdk_handler, metallib_handler  # noqa: E402
from opencore_legacy_patcher.sys_patch.mount import mount, snapshot  # noqa: E402
from opencore_legacy_patcher.sys_patch.utilities import dmg_mount, kdk_merge  # noqa: E402


class OSConversionTests(unittest.TestCase):
    def test_os_to_kernel_mapping_through_tahoe_and_beyond(self):
        self.assertEqual(os_data.os_conversion.os_to_kernel("10.15.7"), 19)
        self.assertEqual(os_data.os_conversion.os_to_kernel("11.0"), 20)
        self.assertEqual(os_data.os_conversion.os_to_kernel("15.2"), 24)
        self.assertEqual(os_data.os_conversion.os_to_kernel("26.0"), 25)
        self.assertEqual(os_data.os_conversion.os_to_kernel("26.1"), 25)
        self.assertEqual(os_data.os_conversion.os_to_kernel("27.0"), 26)

    def test_kernel_to_os_mapping_through_tahoe_and_beyond(self):
        self.assertEqual(os_data.os_conversion.kernel_to_os(19), "10.15")
        self.assertEqual(os_data.os_conversion.kernel_to_os(20), "11")
        self.assertEqual(os_data.os_conversion.kernel_to_os(24), "15")
        self.assertEqual(os_data.os_conversion.kernel_to_os(25), "26")
        self.assertEqual(os_data.os_conversion.kernel_to_os(26), "27")

    def test_os_to_kernel_minor_mapping(self):
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("11.0"), 0)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("11.3"), 4)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("11.7"), 6)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("12.5"), 6)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("13.3"), 4)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("14.4"), 4)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("15.2"), 2)
        self.assertEqual(os_data.os_conversion.os_to_kernel_minor("26.1"), 1)


class OSProbeOfflineTests(unittest.TestCase):
    def _write_plist(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(payload, f)

    def test_detects_target_version_build_and_system_kext_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "target_root"
            self._write_plist(
                root / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "15.2", "ProductBuildVersion": "24C101"},
            )
            self._write_plist(
                root / "System/Library/Extensions/System.kext/Contents/Info.plist",
                {"CFBundleVersion": "24.2.0", "CFBundleShortVersionString": "24.2.0"},
            )

            probe = os_probe.OSProbe(target_root=root)
            self.assertEqual(probe.detect_os_version(), "15.2")
            self.assertEqual(probe.detect_os_build(), "24C101")
            self.assertEqual(probe.detect_kernel_major(), 24)
            self.assertEqual(probe.detect_kernel_minor(), 2)

    def test_falls_back_to_os_conversion_when_system_kext_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "target_root"
            self._write_plist(
                root / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "26.1", "ProductBuildVersion": "25B500"},
            )

            probe = os_probe.OSProbe(target_root=root)
            self.assertEqual(probe.detect_os_version(), "26.1")
            self.assertEqual(probe.detect_kernel_major(), 25)
            self.assertEqual(probe.detect_kernel_minor(), 1)

    def test_detects_preboot_cryptex_rsr_version_and_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "target_root"
            preboot = Path(tmp) / "preboot"
            self._write_plist(
                root / "System/Library/CoreServices/SystemVersion.plist",
                {"ProductVersion": "14.4", "ProductBuildVersion": "23E214"},
            )
            self._write_plist(
                preboot / "VOLUME-UUID-1234/cryptex1/current/SystemVersion.plist",
                {"ProductVersion": "14.4.1", "ProductBuildVersion": "23E224a"},
            )

            probe = os_probe.OSProbe(target_root=root, preboot_root=preboot)
            self.assertEqual(probe.detect_os_version(rsr=False), "14.4")
            self.assertEqual(probe.detect_os_version(rsr=True), "14.4.1")
            self.assertEqual(probe.detect_os_build(rsr=True), "23E224a")


class APFSVolumeResolverAndMountTests(unittest.TestCase):
    def test_strip_snapshot_suffix(self):
        self.assertEqual(mount._strip_snapshot_suffix("disk3s1s1"), "disk3s1")
        self.assertEqual(mount._strip_snapshot_suffix("disk10s1s10"), "disk10s1")
        self.assertEqual(mount._strip_snapshot_suffix("disk3s1"), "disk3s1")

    def _build_apfs_list_plist(self) -> bytes:
        data = {
            "Containers": [
                {
                    "ContainerReference": "disk3",
                    "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk3s1",
                            "Name": "Macintosh HD",
                            "Roles": ["System"],
                            "VolumeGroupUUID": "VG-UUID-1",
                            "APFSVolumeUUID": "SYS-UUID-1",
                            "Encryption": False,
                        },
                        {
                            "DeviceIdentifier": "disk3s5",
                            "Name": "Macintosh HD - Data",
                            "Roles": ["Data"],
                            "VolumeGroupUUID": "VG-UUID-1",
                            "APFSVolumeUUID": "DATA-UUID-1",
                            "Encryption": True,
                            "FileVault": True,
                            "Locked": True,
                        },
                        {
                            "DeviceIdentifier": "disk3s2",
                            "Name": "Preboot",
                            "Roles": ["Preboot"],
                            "APFSVolumeUUID": "PREBOOT-UUID-1",
                        },
                    ],
                }
            ]
        }
        return plistlib.dumps(data)

    def test_discover_and_resolve_and_unlock_volume_group(self):
        apfs_plist = self._build_apfs_list_plist()

        diskutil_info_map = {
            "disk3s1": plistlib.dumps({"MountPoint": "/Volumes/Macintosh HD", "VolumeName": "Macintosh HD"}),
            "disk3s5": plistlib.dumps({"MountPoint": "", "VolumeName": "Macintosh HD - Data", "FileVault": True, "Locked": True}),
            "disk3s2": plistlib.dumps({"MountPoint": "/Volumes/Preboot", "VolumeName": "Preboot"}),
        }

        unlocked_state = {"unlocked": False}

        unhandled_cmds = []

        def fake_run(cmd, *args, **kwargs):
            prog = os.path.basename(cmd[0]) if cmd else ""
            if prog == "diskutil" and cmd[1:4] == ["apfs", "list", "-plist"]:
                if not unlocked_state["unlocked"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=apfs_plist, stderr=b"")
                modified = plistlib.loads(apfs_plist)
                modified["Containers"][0]["Volumes"][1]["Locked"] = False
                return subprocess.CompletedProcess(cmd, 0, stdout=plistlib.dumps(modified), stderr=b"")
            if prog == "diskutil" and cmd[1:3] == ["info", "-plist"]:
                dev = cmd[3]
                if dev == "disk3s5" and unlocked_state["unlocked"]:
                    payload = plistlib.dumps({"MountPoint": "/Volumes/Macintosh HD - Data", "VolumeName": "Macintosh HD - Data", "FileVault": True, "Locked": False})
                else:
                    payload = diskutil_info_map.get(dev, plistlib.dumps({}))
                return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")
            if prog == "diskutil" and cmd[1:3] == ["apfs", "unlockVolume"]:
                unlocked_state["unlocked"] = True
                return subprocess.CompletedProcess(cmd, 0, stdout=b"Unlocked\n", stderr=b"")
            if prog == "mount":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"/dev/disk1s1s1 on / (apfs, sealed, local, read-only)\n", stderr=b"")
            unhandled_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.subprocess.run", side_effect=fake_run), \
             mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.subprocess_wrapper.run_as_root", side_effect=fake_run):
            resolver = mount.APFSVolumeResolver()
            groups = resolver.discover_volume_groups(include_live_root=False)
            self.assertEqual(len(groups), 1, f"unhandled_cmds={unhandled_cmds}")
            group = groups[0]
            self.assertEqual(group.system_identifier, "disk3s1")
            self.assertEqual(group.data_identifier, "disk3s5")
            self.assertEqual(group.preboot_identifier, "disk3s2")
            self.assertTrue(group.filevault_enabled)
            self.assertTrue(group.data_locked)

            # Resolve by path, dev, snapshot dev, UUID, and name
            self.assertEqual(resolver.resolve_target("/Volumes/Macintosh HD").system_identifier, "disk3s1")
            self.assertEqual(resolver.resolve_target("disk3s1").system_identifier, "disk3s1")
            self.assertEqual(resolver.resolve_target("disk3s1s1").system_identifier, "disk3s1")
            self.assertEqual(resolver.resolve_target("SYS-UUID-1").system_identifier, "disk3s1")
            self.assertEqual(resolver.resolve_target("Macintosh HD").system_identifier, "disk3s1")

            with self.assertRaises(RuntimeError):
                resolver.resolve_target("NonExistentVolume")

            refreshed = resolver.unlock_data_volume(group, passphrase="secret")
            self.assertFalse(refreshed.data_locked)
            self.assertEqual(refreshed.data_mount, "/Volumes/Macintosh HD - Data")

    def test_ambiguous_volume_name_resolution_raises(self):
        dup_plist = plistlib.dumps({
            "Containers": [{
                "ContainerReference": "disk3",
                "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
                "Volumes": [
                    {"DeviceIdentifier": "disk3s1", "Name": "macOS", "Roles": ["System"], "VolumeGroupUUID": "VG-1", "APFSVolumeUUID": "U-1"},
                    {"DeviceIdentifier": "disk3s2", "Name": "macOS - Data", "Roles": ["Data"], "VolumeGroupUUID": "VG-1", "APFSVolumeUUID": "U-2"},
                    {"DeviceIdentifier": "disk3s3", "Name": "macOS", "Roles": ["System"], "VolumeGroupUUID": "VG-2", "APFSVolumeUUID": "U-3"},
                    {"DeviceIdentifier": "disk3s4", "Name": "macOS - Data", "Roles": ["Data"], "VolumeGroupUUID": "VG-2", "APFSVolumeUUID": "U-4"},
                ],
            }]
        })

        def fake_run(cmd, *args, **kwargs):
            prog = os.path.basename(cmd[0]) if cmd else ""
            if prog == "diskutil" and cmd[1:4] == ["apfs", "list", "-plist"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=dup_plist, stderr=b"")
            if prog == "diskutil" and cmd[1:3] == ["info", "-plist"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=plistlib.dumps({"MountPoint": ""}), stderr=b"")
            if prog == "mount":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"/dev/disk1s1s1 on / (apfs)\n", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.subprocess.run", side_effect=fake_run):
            resolver = mount.APFSVolumeResolver()
            groups = resolver.discover_volume_groups(include_live_root=False)
            self.assertEqual(len(groups), 2)
            with self.assertRaises(RuntimeError):
                resolver.resolve_target("macOS")

    def test_offline_root_volume_mount_unlocks_filevault_and_cleans_up(self):
        locked_group = mount.TargetVolumeGroup(
            container_disk="disk3",
            system_identifier="disk3s1",
            volume_name="Macintosh HD",
            data_identifier="disk3s5",
            data_volume_name="Macintosh HD - Data",
            preboot_identifier="disk3s2",
            system_mount="/Volumes/Macintosh HD",
            data_mount=None,
            preboot_mount="/Volumes/Preboot",
            filevault_enabled=True,
            data_locked=True,
            is_live_booted=False,
        )
        unlocked_group = mount.TargetVolumeGroup(
            container_disk="disk3",
            system_identifier="disk3s1",
            volume_name="Macintosh HD",
            data_identifier="disk3s5",
            data_volume_name="Macintosh HD - Data",
            preboot_identifier="disk3s2",
            system_mount="/Volumes/Macintosh HD",
            data_mount="/Volumes/Macintosh HD - Data",
            preboot_mount="/Volumes/Preboot",
            filevault_enabled=True,
            data_locked=False,
            is_live_booted=False,
        )

        commands = []

        def fake_run(cmd, *args, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            stage_dir = os.path.join(tmp, "ocpbp_stage")
            with mock.patch.object(mount.APFSVolumeResolver, "resolve_target", return_value=locked_group), \
                 mock.patch.object(mount.APFSVolumeResolver, "unlock_data_volume", return_value=unlocked_group) as mock_unlock, \
                 mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.tempfile.mkdtemp", return_value=stage_dir), \
                 mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.subprocess.run", side_effect=fake_run), \
                 mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.subprocess_wrapper.run_as_root", side_effect=fake_run), \
                 mock.patch("opencore_legacy_patcher.sys_patch.mount.mount.Path.exists", return_value=True):
                rvm = mount.RootVolumeMount(xnu_major=24, target_root="/Volumes/Macintosh HD", passphrase="pw")
                self.assertTrue(rvm.is_offline_target)
                mounted = rvm.mount()
                self.assertEqual(mounted, stage_dir)
                self.assertNotEqual(mounted, "/System/Volumes/Update/mnt1")
                self.assertEqual(rvm.data_mount, "/Volumes/Macintosh HD - Data")
                mock_unlock.assert_called_once()
                self.assertTrue(rvm.unmount())

        self.assertTrue(any("mount_apfs" in " ".join(str(c) for c in cmd) or "mount" in " ".join(str(c) for c in cmd) for cmd in commands))

    def test_snapshot_guards_missing_mount_path(self):
        snap = snapshot.APFSSnapshot(xnu_major=24, mount_path=None)
        self.assertFalse(snap.create_snapshot())
        self.assertFalse(snap.revert_snapshot())

    def test_snapshot_runs_bless_when_mount_path_provided(self):
        commands = []

        def fake_rar(cmd, *args, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        snap = snapshot.APFSSnapshot(xnu_major=24, mount_path="/tmp/ocpbp_stage")
        with mock.patch("opencore_legacy_patcher.sys_patch.mount.snapshot.subprocess_wrapper.run_as_root", side_effect=fake_rar):
            self.assertTrue(snap.create_snapshot())
            self.assertTrue(snap.revert_snapshot())
        self.assertTrue(any("--create-snapshot" in [str(x) for x in c] for c in commands))
        self.assertTrue(any("--last-sealed-snapshot" in [str(x) for x in c] for c in commands))


class OfflineAssetsKDKAndMetallibTests(unittest.TestCase):
    def _simulate_root_fs_cmd(self, cmd, expanded_builder=None):
        prog = os.path.basename(cmd[0]) if cmd else ""
        if prog == "mkdir" and "-p" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if prog == "rm":
            shutil.rmtree(cmd[-1], ignore_errors=True)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if prog == "mv":
            shutil.move(cmd[-2], cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if prog == "pkgutil" and cmd[1] == "--expand-full" and expanded_builder is not None:
            expanded_builder(Path(cmd[3]))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    def test_offline_kdk_lookup_and_non_destructive_corrupt_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            target_data = Path(tmp) / "TargetData"
            assets_dir.mkdir(parents=True)
            kdk_install_dir = target_data / "Library/Developer/KDKs"

            # Corrupt .kdk inside offline assets must NOT be deleted even with passive=False
            corrupt_kdk = assets_dir / "KDK_15.2_24C101.kdk"
            corrupt_kdk.mkdir()
            (corrupt_kdk / "marker.txt").write_text("keep me")

            # Fallback generic KDK.pkg present
            generic_pkg = assets_dir / "KDK.pkg"
            generic_pkg.write_bytes(b"pkg")

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False), \
                 mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess_wrapper.run_as_root") as mock_rar:
                obj = kdk_handler.KernelDebugKitObject(
                    constants.Constants(),
                    "24C101",
                    "15.2",
                    passive=False,
                    kdk_install_path=kdk_install_dir,
                    offline_assets_dir=assets_dir,
                    target_volume=target_data,
                )
                mock_rar.assert_not_called()

            self.assertTrue(corrupt_kdk.exists(), "Offline corrupt .kdk must not be deleted")
            self.assertTrue((corrupt_kdk / "marker.txt").exists())
            self.assertTrue(obj.kdk_available_offline)
            self.assertTrue(obj.kdk_available)
            self.assertEqual(Path(obj.kdk_offline_asset_path), generic_pkg)
            self.assertEqual(obj.kdk_offline_asset_type, "pkg")

    def test_offline_kdk_lookup_via_kdkinfo_plist_and_dmg(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            target_data = Path(tmp) / "TargetData"
            assets_dir.mkdir(parents=True)
            kdk_install_dir = target_data / "Library/Developer/KDKs"

            with open(assets_dir / "KDKInfo.plist", "wb") as f:
                plistlib.dump({"version": "15.2", "build": "24C101"}, f)
            dmg_path = assets_dir / "KernelDebugKit.dmg"
            dmg_path.write_bytes(b"dmg")

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False):
                obj = kdk_handler.KernelDebugKitObject(
                    constants.Constants(),
                    "24C101",
                    "15.2",
                    passive=True,
                    kdk_install_path=kdk_install_dir,
                    offline_assets_dir=assets_dir,
                    target_volume=target_data,
                )

            self.assertTrue(obj.kdk_available_offline)
            self.assertEqual(obj.kdk_offline_asset_type, "dmg")
            self.assertEqual(Path(obj.kdk_offline_asset_path), dmg_path)

    def test_install_kdk_pkg_extracts_inside_target_dest_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_data = Path(tmp) / "TargetData"
            dest_dir = target_data / "Library/Developer/KDKs"
            fake_pkg = Path(tmp) / "KDK.pkg"
            fake_pkg.write_bytes(b"fake")

            captured_cmds = []

            def build_expanded(expanded_dir: Path) -> None:
                kdk_dir = expanded_dir / "KDK.pkg/Payload/Library/Developer/KDKs/KDK_15.2_24C101.kdk"
                (kdk_dir / "System/Library/Extensions").mkdir(parents=True, exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                captured_cmds.append(cmd)
                return self._simulate_root_fs_cmd(cmd, expanded_builder=build_expanded)

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess_wrapper.run_as_root", side_effect=fake_run):
                utils = kdk_handler.KernelDebugKitUtilities()
                ok = utils.install_kdk_pkg(fake_pkg, target_volume=target_data, kdk_install_path=dest_dir)

            self.assertTrue(ok)
            self.assertTrue((dest_dir / "KDK_15.2_24C101.kdk/System/Library/Extensions").exists())
            expand_cmds = [c for c in captured_cmds if os.path.basename(c[0]) == "pkgutil" and c[1] == "--expand-full"]
            self.assertEqual(len(expand_cmds), 1)
            expanded_arg = Path(expand_cmds[0][3])
            self.assertEqual(expanded_arg.parent.parent, dest_dir)
            self.assertFalse(expanded_arg.parent.exists(), "Staging directory must be cleaned up")

    def test_install_kdk_dmg_targets_custom_kdk_install_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_data = Path(tmp) / "TargetData"
            dest_dir = target_data / "Library/Developer/KDKs"
            dmg_path = Path(tmp) / "KernelDebugKit.dmg"
            dmg_path.write_bytes(b"dmg")
            mnt_dir = Path(tmp) / "kdk_dmg_mnt"

            def fake_rar(cmd, *args, **kwargs):
                prog = os.path.basename(cmd[0]) if cmd else ""
                if prog == "hdiutil" and cmd[1] == "attach":
                    mp = Path(cmd[cmd.index("-mountpoint") + 1])
                    mp.mkdir(parents=True, exist_ok=True)
                    (mp / "KernelDebugKit.pkg").write_bytes(b"pkg")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.tempfile.TemporaryDirectory") as mock_td, \
                 mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess_wrapper.run_as_root", side_effect=fake_rar), \
                 mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")), \
                 mock.patch.object(kdk_handler.KernelDebugKitUtilities, "install_kdk_pkg", return_value=True) as mock_install_pkg:
                mock_td.return_value.__enter__.return_value = str(mnt_dir)
                utils = kdk_handler.KernelDebugKitUtilities()
                ok = utils.install_kdk_dmg(dmg_path, target_volume=target_data, kdk_install_path=dest_dir)

            self.assertTrue(ok)
            mock_install_pkg.assert_called_once()
            _, kwargs = mock_install_pkg.call_args
            self.assertEqual(Path(kwargs["target_volume"]), target_data)
            self.assertEqual(Path(kwargs["kdk_install_path"]), dest_dir)

    def test_offline_metallib_lookup_and_pkg_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            target_data = Path(tmp) / "TargetData"
            dest_dir = target_data / "Library/Application Support/Dortania/MetallibSupportPkg"
            assets_dir.mkdir(parents=True)
            pkg_path = assets_dir / "MetallibSupportPkg.pkg"
            pkg_path.write_bytes(b"pkg")

            with mock.patch("opencore_legacy_patcher.support.metallib_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False):
                obj = metallib_handler.MetalLibraryObject(
                    constants.Constants(),
                    "24C101",
                    "15.2",
                    passive=True,
                    metallib_install_path=dest_dir,
                    offline_assets_dir=assets_dir,
                    target_volume=target_data,
                )

            self.assertTrue(obj.metallib_available_offline)
            self.assertTrue(obj.metallib_available)
            self.assertEqual(Path(obj.metallib_offline_asset_path), pkg_path)

            captured_cmds = []

            def build_expanded(expanded_dir: Path) -> None:
                ver_dir = expanded_dir / "Payload/Library/Application Support/Dortania/MetallibSupportPkg/15.2-24C101"
                (ver_dir / "System/Library/Frameworks").mkdir(parents=True, exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                captured_cmds.append(cmd)
                return self._simulate_root_fs_cmd(cmd, expanded_builder=build_expanded)

            with mock.patch("opencore_legacy_patcher.support.metallib_handler.subprocess_wrapper.run_as_root", side_effect=fake_run):
                ok = obj.install_metallib(metallib=pkg_path, target_volume=target_data, metallib_install_path=dest_dir)

            self.assertTrue(ok)
            self.assertTrue((dest_dir / "15.2-24C101/System/Library/Frameworks").exists())
            expand_cmds = [c for c in captured_cmds if os.path.basename(c[0]) == "pkgutil" and c[1] == "--expand-full"]
            self.assertEqual(len(expand_cmds), 1)
            expanded_arg = Path(expand_cmds[0][3])
            self.assertEqual(expanded_arg.parent.parent, dest_dir)
            self.assertFalse(expanded_arg.parent.exists(), "Staging directory must be cleaned up")

    def test_offline_metallib_component_pkg_directly_under_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            target_data = Path(tmp) / "TargetData"
            dest_dir = target_data / "Library/Application Support/Dortania/MetallibSupportPkg"
            assets_dir.mkdir(parents=True)
            with open(assets_dir / "MetallibInfo.plist", "wb") as f:
                plistlib.dump({"version": "15.2", "build": "24C101"}, f)
            pkg_path = assets_dir / "MetallibSupportPkg-custom.pkg"
            pkg_path.write_bytes(b"pkg")

            with mock.patch("opencore_legacy_patcher.support.metallib_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False):
                obj = metallib_handler.MetalLibraryObject(
                    constants.Constants(),
                    "24C101",
                    "15.2",
                    passive=True,
                    metallib_install_path=dest_dir,
                    offline_assets_dir=assets_dir,
                    target_volume=target_data,
                )

            self.assertTrue(obj.metallib_available_offline)

            def build_expanded(expanded_dir: Path) -> None:
                ver_dir = expanded_dir / "Payload/15.2-24C101"
                (ver_dir / "System/Library/Frameworks").mkdir(parents=True, exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                return self._simulate_root_fs_cmd(cmd, expanded_builder=build_expanded)

            with mock.patch("opencore_legacy_patcher.support.metallib_handler.subprocess_wrapper.run_as_root", side_effect=fake_run):
                ok = obj.install_metallib(metallib=pkg_path, target_volume=target_data, metallib_install_path=dest_dir)

            self.assertTrue(ok)
            self.assertTrue((dest_dir / "15.2-24C101/System/Library/Frameworks").exists())


class KDKMergeAndDMGMountTests(unittest.TestCase):
    def test_patcher_support_pkg_mount_unpacked_dir_and_dmg_cleanup(self):
        c = constants.Constants()
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            unpacked = assets_dir / "Universal-Binaries"
            (unpacked / "System/Library").mkdir(parents=True)

            mounter = dmg_mount.PatcherSupportPkgMount(c, offline_assets_dir=assets_dir)
            self.assertTrue(mounter.mount())
            self.assertEqual(Path(c.payload_local_binaries_root_path), unpacked)
            mounter.unmount()
            self.assertIsNone(c._payload_local_binaries_root_path_override)

        # DMG fallback with writable temp mountpoint & shadow cleanup
        c2 = constants.Constants()
        c2.payload_path = Path(tmp_ro := tempfile.mkdtemp(prefix="ro_payload_")) if False else Path("/nonexistent_read_only_payload")
        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp) / "OCPBP"
            assets_dir.mkdir(parents=True)
            dmg_file = assets_dir / "Universal-Binaries.dmg"
            dmg_file.write_bytes(b"dmg")
            temp_base = Path(tmp) / "ub_base"
            temp_base.mkdir(parents=True)
            expected_mnt = temp_base / "Universal-Binaries"

            calls = []

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                prog = os.path.basename(cmd[0]) if cmd else ""
                if prog == "hdiutil" and cmd[1] == "attach":
                    expected_mnt.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.sys_patch.utilities.dmg_mount.tempfile.mkdtemp", return_value=str(temp_base)), \
                 mock.patch("opencore_legacy_patcher.sys_patch.utilities.dmg_mount.subprocess.run", side_effect=fake_run):
                m2 = dmg_mount.PatcherSupportPkgMount(c2, offline_assets_dir=assets_dir)
                self.assertTrue(m2.mount())
                self.assertEqual(Path(c2.payload_local_binaries_root_path), expected_mnt)
                m2.unmount()

            self.assertTrue(any(os.path.basename(cmd[0]) == "hdiutil" and cmd[1] == "detach" for cmd in calls))
            self.assertIsNone(c2._payload_local_binaries_root_path_override)

    def test_kdk_merge_checks_target_plist_and_skips_if_matching(self):
        c = constants.Constants()
        c.detected_os = 24
        with tempfile.TemporaryDirectory() as tmp:
            mount_loc = Path(tmp) / "target_root"
            target_data = Path(tmp) / "target_data"
            kdk_dir = target_data / "Library/Developer/KDKs/KDK_15.2_24C101.kdk"
            plist_path = mount_loc / "System/Library/CoreServices/OpenCore-Legacy-Patcher.plist"
            plist_path.parent.mkdir(parents=True)
            with open(plist_path, "wb") as f:
                plistlib.dump({"Kernel Debug Kit Used": str(kdk_dir)}, f)

            # Matching KDK assumes System.kext/PlugIns/Libkern.kext/Libkern was previously merged
            libkern = mount_loc / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern"
            libkern.parent.mkdir(parents=True)
            libkern.write_bytes(b"libkern")

            (kdk_dir / "System/Library/Extensions").mkdir(parents=True)
            (kdk_dir / "System/Library/Kernels/kernel").parent.mkdir(parents=True)
            (kdk_dir / "System/Library/Kernels/kernel").write_bytes(b"k")

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.KernelDebugKitObject") as mock_kdk_cls:
                mock_kdk_cls.return_value = types.SimpleNamespace(
                    success=True,
                    kdk_already_installed=True,
                    kdk_installed_path=str(kdk_dir),
                    kdk_available_offline=False,
                    kdk_offline_asset_path=None,
                    kdk_offline_asset_type=None,
                    kdk_url="",
                    kdk_url_expected_size=0,
                    kdk_download_path=Path(tmp) / "dl.dmg",
                    kdk_closest_match_url="",
                    kdk_closest_match_url_expected_size=0,
                    validate_kdk_checksum=lambda *a, **kw: True,
                )
                merger = kdk_merge.KernelDebugKitMerge(
                    global_constants=c,
                    mount_location=str(mount_loc),
                    skip_root_kmutil_requirement=False,
                    target_data_mount=str(target_data),
                    target_os_build="24C101",
                    target_os_version="15.2",
                    target_os_major=24,
                )
                with mock.patch("opencore_legacy_patcher.sys_patch.utilities.kdk_merge.subprocess_wrapper.run_as_root") as mock_rar:
                    result = merger.merge(save_hid_cs=False)
                    self.assertEqual(Path(result), kdk_dir)
                    mock_rar.assert_not_called()

    def test_kdk_merge_extracts_offline_pkg_and_uses_scratch_hid_cs_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = constants.Constants()
            c.detected_os = 24
            c.payload_path = Path(tmp) / "nonexistent_read_only_payload"

            mount_loc = Path(tmp) / "target_root"
            target_data = Path(tmp) / "target_data"
            assets_dir = Path(tmp) / "OCPBP"
            scratch_hid_dir = Path(tmp) / "hid_scratch"
            scratch_hid_dir.mkdir(parents=True)
            assets_dir.mkdir(parents=True)
            (assets_dir / "KDK.pkg").write_bytes(b"pkg")

            # Existing IOHIDEventDriver _CodeSignature on target_root
            cs_dir = mount_loc / "System/Library/Extensions/IOHIDFamily.kext/Contents/PlugIns/IOHIDEventDriver.kext/Contents/_CodeSignature"
            cs_dir.mkdir(parents=True)
            (cs_dir / "CodeResources").write_text("sig")
            (mount_loc / "System/Library/Extensions").mkdir(parents=True, exist_ok=True)

            kdk_dest = target_data / "Library/Developer/KDKs" / "KDK_15.2_24C101.kdk"
            captured_cmds = []

            def fake_install_kdk_pkg(pkg_path, target_volume=None, kdk_install_path=None):
                (kdk_dest / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext").mkdir(parents=True, exist_ok=True)
                (kdk_dest / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern").write_bytes(b"libkern")
                (kdk_dest / "System/Library/CoreServices").mkdir(parents=True, exist_ok=True)
                with open(kdk_dest / "System/Library/CoreServices/SystemVersion.plist", "wb") as f:
                    plistlib.dump({"ProductBuildVersion": "24C101", "ProductVersion": "15.2"}, f)
                return True

            def fake_rar(cmd, *args, **kwargs):
                captured_cmds.append(cmd)
                prog = os.path.basename(cmd[0]) if cmd else ""
                if prog == "rsync":
                    libkern = mount_loc / "System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern"
                    libkern.parent.mkdir(parents=True, exist_ok=True)
                    libkern.write_bytes(b"libkern")
                elif prog in ("cp", "ditto") and len(cmd) >= 3:
                    src = Path(cmd[-2])
                    dst = Path(cmd[-1])
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                elif prog == "rm":
                    shutil.rmtree(cmd[-1], ignore_errors=True)
                elif prog == "mkdir":
                    Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            pkgutil_cmds = []

            def fake_kdk_subproc(cmd, *args, **kwargs):
                pkgutil_cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

            with mock.patch("opencore_legacy_patcher.support.kdk_handler.network_handler.NetworkUtilities.verify_network_connection", return_value=False), \
                 mock.patch("opencore_legacy_patcher.support.kdk_handler.subprocess.run", side_effect=fake_kdk_subproc), \
                 mock.patch.object(kdk_handler.KernelDebugKitObject, "_local_kdk_valid_legacy", return_value=True), \
                 mock.patch.object(kdk_handler.KernelDebugKitUtilities, "install_kdk_pkg", side_effect=fake_install_kdk_pkg) as mock_pkg, \
                 mock.patch("opencore_legacy_patcher.sys_patch.utilities.kdk_merge.tempfile.mkdtemp", return_value=str(scratch_hid_dir)), \
                 mock.patch("opencore_legacy_patcher.sys_patch.utilities.kdk_merge.subprocess_wrapper.run_as_root", side_effect=fake_rar):
                merger = kdk_merge.KernelDebugKitMerge(
                    global_constants=c,
                    mount_location=str(mount_loc),
                    skip_root_kmutil_requirement=False,
                    target_data_mount=str(target_data),
                    target_os_build="24C101",
                    target_os_version="15.2",
                    target_os_major=24,
                    offline_assets_dir=assets_dir,
                )
                merged_kdk = merger.merge(save_hid_cs=True)

            self.assertEqual(Path(merged_kdk), kdk_dest)
            mock_pkg.assert_called_once()
            self.assertTrue(any("--volume" in cmd and str(target_data) in cmd for cmd in pkgutil_cmds))
            self.assertTrue(any(os.path.basename(cmd[0]) == "rsync" and str(mount_loc) in " ".join(str(x) for x in cmd) for cmd in captured_cmds))
            self.assertTrue(any(str(scratch_hid_dir) in " ".join(str(x) for x in cmd) for cmd in captured_cmds))


if __name__ == "__main__":
    unittest.main()


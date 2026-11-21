import ast
import os
import plistlib
import runpy
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

    for mod_name in ("macos_pkg_builder", "mac_signing_buddy", "applescript"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    if "macos_pkg_builder.utilities" not in sys.modules:
        mpb = sys.modules["macos_pkg_builder"]
        mpb_utils = types.ModuleType("macos_pkg_builder.utilities")
        mpb_signing = types.ModuleType("macos_pkg_builder.utilities.signing")
        mpb_utils.signing = mpb_signing
        mpb.utilities = mpb_utils
        sys.modules["macos_pkg_builder.utilities"] = mpb_utils
        sys.modules["macos_pkg_builder.utilities.signing"] = mpb_signing


class _AutoWxModule(types.ModuleType):
    def __init__(self, name: str):
        super().__init__(name)
        self.__path__ = []
        self.Frame = type("Frame", (), {})
        self.Dialog = type("Dialog", (), {})
        self.CheckBox = type("CheckBox", (), {})
        self.Event = object
        self.YES = 1
        self.NO = 2
        self.OK = 4
        self.ID_YES = 1
        self.YES_NO = 1
        self.NO_DEFAULT = 2
        self.ICON_WARNING = 4
        self.ICON_QUESTION = 8
        self.ICON_INFORMATION = 16
        self.ICON_ERROR = 32
        self.FONTWEIGHT_BOLD = 1
        self.FONTWEIGHT_NORMAL = 0
        self.HORIZONTAL = 1
        self.DEFAULT_FRAME_STYLE = 0
        self.RESIZE_BORDER = 0
        self.MAXIMIZE_BOX = 0
        self.MessageBox = lambda *a, **kw: self.NO

    def __getattr__(self, item: str):
        return type(item, (), {})


_install_linux_stubs_if_needed()

REPO_ROOT = Path(__file__).resolve().parent.parent


class Phase5PackagingAndCITests(unittest.TestCase):
    def test_ocpbp_cli_entrypoint_invokes_headless_main_without_wx(self):
        entry_file = REPO_ROOT / "ocpbp_cli.py"
        self.assertTrue(entry_file.is_file())
        source = entry_file.read_text(encoding="utf-8")
        self.assertIn("application_entry.main(headless=True)", source)
        self.assertNotIn("wx_gui", source)
        self.assertNotIn("import wx", source)

        with mock.patch("opencore_legacy_patcher.application_entry.main") as main_mock:
            runpy.run_path(str(entry_file), run_name="__main__")
        main_mock.assert_called_once_with(headless=True)

    def test_pyinstaller_spec_excludes_gui_and_builds_universal2_console_binary(self):
        spec_file = REPO_ROOT / "OpenCore-Post-Boot-Patcher.spec"
        self.assertTrue(spec_file.is_file())
        spec_src = spec_file.read_text(encoding="utf-8")
        # Ensure AST syntax is clean
        ast.parse(spec_src)

        self.assertIn('"ocpbp_cli.py"', spec_src)
        self.assertIn('name="ocpbp"', spec_src)
        self.assertIn("console=True", spec_src)
        self.assertIn('target_arch="universal2"', spec_src)
        for excluded in ("wx", "wxPython", "applescript", "AppKit", "opencore_legacy_patcher.wx_gui"):
            self.assertIn(f'"{excluded}"', spec_src)

    def test_application_builder_builds_headless_ocpbp_and_embeds_in_gui_bundle(self):
        from ci_tooling.build_modules import application

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                dist_dir = Path(tmp) / "dist"
                dist_dir.mkdir()
                fake_ocpbp = dist_dir / "ocpbp"

                def _fake_pyinstaller(cmd, **kw):
                    self.assertIn("./OpenCore-Post-Boot-Patcher.spec", cmd)
                    fake_ocpbp.write_bytes(b"\xcf\xfa\xed\xfe\x00\x0D\x0A\x00rest")
                    return types.SimpleNamespace(returncode=0)

                with mock.patch("ci_tooling.build_modules.application.subprocess_wrapper.run_and_verify", side_effect=_fake_pyinstaller):
                    application.GenerateHeadlessApplication().generate()

                self.assertIn(b"\x00\x0A\x0A\x00", fake_ocpbp.read_bytes())

                app_out = dist_dir / "OpenCore-Patcher.app"
                (app_out / "Contents/MacOS").mkdir(parents=True)
                (app_out / "Contents/Resources").mkdir(parents=True)
                Path("payloads/Icon/AppIcons").mkdir(parents=True)
                Path("payloads/Icon/AppIcons/OC-Patcher.icns").write_bytes(b"icns")
                Path("payloads/Icon/AppIcons/Assets.car").write_bytes(b"car")

                copied_calls = []
                with (
                    mock.patch("ci_tooling.build_modules.application.generate_copy_arguments", side_effect=lambda s, d: ["cp", str(s), str(d)]),
                    mock.patch("ci_tooling.build_modules.application.subprocess_wrapper.run_and_verify", side_effect=lambda cmd, **kw: copied_calls.append(cmd) or types.SimpleNamespace(returncode=0)),
                ):
                    gen_app = application.GenerateApplication()
                    gen_app._application_output = app_out
                    gen_app._embed_resources()

                self.assertTrue(
                    any("dist/ocpbp" in c[1] and c[2].endswith("Contents/MacOS/ocpbp") for c in copied_calls),
                    f"Expected Contents/MacOS/ocpbp copy in {copied_calls}",
                )
            finally:
                os.chdir(old_cwd)

    def test_build_project_command_and_workflows_cover_tests_and_ocpbp(self):
        build_script = (REPO_ROOT / "Build-Project.command").read_text(encoding="utf-8")
        self.assertIn("--prepare-ocpbp", build_script)
        self.assertIn("GenerateHeadlessApplication", build_script)
        self.assertLess(
            build_script.index("GenerateHeadlessApplication"),
            build_script.index("GenerateApplication"),
            "Headless ocpbp must be built before OpenCore-Patcher.app so it can be embedded",
        )

        validate_yml = (REPO_ROOT / ".github/workflows/validate-external.yml").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests", validate_yml)
        self.assertIn("python3 ocpbp_cli.py --validate", validate_yml)
        self.assertIn("python3 OpenCore-Patcher-GUI.command --validate", validate_yml)

        release_yml = (REPO_ROOT / ".github/workflows/build-app-wxpython.yml").read_text(encoding="utf-8")
        self.assertIn("Build-Project.command", release_yml)
        self.assertIn("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3", release_yml)
        self.assertIn("lipo -archs dist/ocpbp", release_yml)
        self.assertIn("ocpbp-macos-universal2.zip", release_yml)
        self.assertIn("AutoPkg-Assets.pkg", release_yml)

    def test_gui_flash_validates_target_model_before_erase_and_uses_partition3(self):
        wx_stub = _AutoWxModule("wx")
        wx_adv = _AutoWxModule("wx.adv")
        wx_html2 = _AutoWxModule("wx.html2")
        wx_stub.adv = wx_adv
        wx_stub.html2 = wx_html2
        stub_modules = {
            "wx": wx_stub,
            "wx.adv": wx_adv,
            "wx.html2": wx_html2,
            "markdown2": types.ModuleType("markdown2"),
        }

        with mock.patch.dict(sys.modules, stub_modules):
            from opencore_legacy_patcher import constants
            from opencore_legacy_patcher.wx_gui import gui_macos_installer_flash

            gconst = constants.Constants()
            gconst.custom_model = None
            gconst.computer = types.SimpleNamespace(real_model="Hackintosh1,1")
            gconst.host_is_hackintosh = True

            frame = gui_macos_installer_flash.macOSInstallerFlashFrame.__new__(
                gui_macos_installer_flash.macOSInstallerFlashFrame
            )
            frame.constants = gconst
            frame.title = "OCPBP"
            frame.build_ocpbp_usb = True
            frame.selected_ocpbp_layout = None
            frame.ocpbp_checkbox = types.SimpleNamespace(GetValue=lambda: True)

            disk = {"identifier": "/dev/disk4", "name": "USB 64GB", "size": 64_000_000_000}
            installer = {"Short Name": "Sequoia", "Path": "/Applications/Install macOS Sequoia.app", "OS": 24}

            with mock.patch("opencore_legacy_patcher.wx_gui.gui_macos_installer_flash.wx.MessageBox") as msgbox:
                frame.on_select_disk(disk, installer)

            msgbox.assert_called_once()
            self.assertIn("Target Model", msgbox.call_args.args[1])

            # Verify _install_installer_pkg writes assets to s3 OCPBP mount without /Library/Packages on s2
            with tempfile.TemporaryDirectory() as tmp:
                s2_mount = Path(tmp) / "Install_macOS"
                s3_mount = Path(tmp) / "OCPBP"
                (s2_mount / "System/Library/CoreServices").mkdir(parents=True)
                s3_mount.mkdir(parents=True)
                with (s2_mount / "System/Library/CoreServices/SystemVersion.plist").open("wb") as handle:
                    plistlib.dump({"ProductVersion": "15.2", "ProductBuildVersion": "24C101"}, handle)

                gconst.computer = types.SimpleNamespace(real_model="MacPro6,1")
                gconst.host_is_hackintosh = False
                frame.build_ocpbp_usb = True

                populate_mock = mock.Mock(return_value={"mount_path": str(s3_mount)})
                metallib_mock = mock.Mock()
                kdk_mock = mock.Mock()
                frame._chainload_metallib = metallib_mock
                frame._kdk_chainload = kdk_mock

                def _grab_mount(part):
                    return str(s2_mount) if part.endswith("s2") else str(s3_mount)

                with (
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_macos_installer_flash.utilities.grab_mount_point_from_disk", side_effect=_grab_mount),
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_macos_installer_flash.macos_installer_handler.InstallerCreation.populate_ocpbp_volume", populate_mock),
                ):
                    frame._install_installer_pkg("disk4")

                self.assertFalse((s2_mount / "Library/Packages").exists())
                populate_mock.assert_called_once()
                self.assertEqual(metallib_mock.call_args.args[2], s3_mount)
                self.assertEqual(kdk_mock.call_args.args[2], s3_mount)

                # Verify _prepare_resources passes ocpbp_enabled=True and layout partition size
                frame.selected_ocpbp_layout = {"installer_size_arg": "19G"}
                with (
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_macos_installer_flash.gui_support.wait_for_thread", side_effect=lambda t: t.join()),
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_macos_installer_flash.macos_installer_handler.InstallerCreation.generate_installer_creation_script", return_value=True) as gen_script,
                ):
                    ok = frame._prepare_resources("/Applications/Install macOS Sequoia.app", "/dev/disk4")
                self.assertTrue(ok)
                gen_script.assert_called_once_with(
                    gconst.payload_path,
                    "/Applications/Install macOS Sequoia.app",
                    "/dev/disk4",
                    ocpbp_enabled=True,
                    installer_partition_size="19G",
                )

    def test_gui_build_enforces_usb_efi_flags_without_mutating_session_constants(self):
        wx_stub = _AutoWxModule("wx")
        wx_adv = _AutoWxModule("wx.adv")
        wx_html2 = _AutoWxModule("wx.html2")
        wx_stub.adv = wx_adv
        wx_stub.html2 = wx_html2
        stub_modules = {
            "wx": wx_stub,
            "wx.adv": wx_adv,
            "wx.html2": wx_html2,
            "markdown2": types.ModuleType("markdown2"),
        }

        with mock.patch.dict(sys.modules, stub_modules):
            from opencore_legacy_patcher import constants
            from opencore_legacy_patcher.ocpbp import esp_sync
            from opencore_legacy_patcher.wx_gui import gui_build

            with tempfile.TemporaryDirectory() as tmp:
                gconst = constants.Constants()
                gconst.current_path = Path(tmp)
                gconst.custom_model = "MacPro6,1"
                gconst.sip_status = True
                gconst.secure_status = True
                gconst.ocpbp_usb_efi_enforce = True

                bf = gui_build.BuildFrame.__new__(gui_build.BuildFrame)
                bf.constants = gconst
                bf.text_box = object()
                bf.build_successful = False

                def _fake_build(model, bconst):
                    self.assertFalse(bconst.sip_status)
                    self.assertFalse(bconst.secure_status)
                    Path(bconst.oc_folder).mkdir(parents=True, exist_ok=True)
                    with Path(bconst.plist_path).open("wb") as handle:
                        plistlib.dump({
                            "NVRAM": {
                                "Add": {esp_sync.APPLE_NVRAM_GUID: {}, esp_sync.OCLP_NVRAM_GUID: {}},
                                "Delete": {},
                            },
                            "Misc": {"Security": {"SecureBootModel": "Default"}},
                            "Kernel": {"Patch": []},
                        }, handle)

                dummy_logger = types.SimpleNamespace(
                    handlers=[None, None, None],
                    addHandler=lambda _h: None,
                    removeHandler=lambda _h: None,
                )
                with (
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_build.logging.getLogger", return_value=dummy_logger),
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_build.gui_support.ThreadHandler", return_value=object()),
                    mock.patch("opencore_legacy_patcher.wx_gui.gui_build.build.BuildOpenCore", side_effect=_fake_build),
                ):
                    bf._build()

                self.assertTrue(bf.build_successful)
                self.assertTrue(gconst.sip_status)
                self.assertTrue(gconst.secure_status)
                self.assertFalse(gconst.ocpbp_usb_efi_enforce)
                inspected = esp_sync.inspect_efi_config(gconst.plist_path)
                self.assertTrue(inspected["ready_for_root_patch"])

    def test_invalid_target_vol_fails_loudly_and_grab_mount_handles_unmounted(self):
        from opencore_legacy_patcher import constants
        from opencore_legacy_patcher.support import utilities
        from opencore_legacy_patcher.sys_patch.patchsets import HardwarePatchsetDetection

        gconst = constants.Constants()
        gconst.computer = types.SimpleNamespace(real_model="MacPro6,1", gpus=[], wifi=None, ethernet=[], usb_controllers=[], pcie_webcam=False, t1_chip=False, internal_keyboard_type="", trackpad_type="", backlight_type="", gmux_version=None, audio_type="", legacy_audio=False, modern_audio=False, webcam_type="", rosetta_active=False)

        with mock.patch("opencore_legacy_patcher.sys_patch.mount.APFSVolumeResolver.resolve_target", side_effect=RuntimeError("not found")):
            with self.assertRaises(RuntimeError):
                HardwarePatchsetDetection(gconst, target_root="/nonexistent/volume")

        with mock.patch("opencore_legacy_patcher.support.utilities.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout=plistlib.dumps({"DeviceIdentifier": "disk4s3"}))):
            self.assertEqual(utilities.grab_mount_point_from_disk("disk4s3"), "")


if __name__ == "__main__":
    unittest.main()

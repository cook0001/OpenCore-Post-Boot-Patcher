"""
arguments.py: CLI argument handling
"""

import sys
import time
import logging
import plistlib
import threading
import subprocess

from pathlib import Path

from . import subprocess_wrapper

from .. import constants

from ..efi_builder import build
from ..sys_patch import sys_patch

from ..datasets import (
    model_array,
    os_data
)

from . import (
    utilities,
    defaults,
    validation
)



# Generic building args
class arguments:

    def __init__(self, global_constants: constants.Constants, parsed_args=None) -> None:
        self.constants: constants.Constants = global_constants

        self.args = parsed_args if parsed_args is not None else utilities.check_cli_args()

        self._parse_arguments()


    def _configure_ocpbp_context(self) -> None:
        """
        Apply optional --model / --offline_assets_dir / /Volumes/OCPBP auto-discovery
        for OCPBP and offline patching commands.
        """
        from ..ocpbp import esp_sync

        if getattr(self.args, "offline_assets_dir", None):
            try:
                discovered = esp_sync.discover_offline_assets_dir(
                    explicit_dir=self.args.offline_assets_dir,
                    global_constants=self.constants,
                )
                self.constants.offline_assets_dir = discovered
            except FileNotFoundError as exc:
                logging.error(str(exc))
                sys.exit(1)
        elif self.constants.offline_assets_dir is None:
            discovered = esp_sync.discover_offline_assets_dir(
                explicit_dir=None,
                global_constants=self.constants,
            )
            if discovered is not None:
                self.constants.offline_assets_dir = discovered

        if getattr(self.args, "model", None) and not self.args.build:
            requested_model = self.args.model
            if (
                requested_model not in model_array.SupportedSMBIOS
                and not getattr(self.args, "support_all", False)
                and not self.constants.allow_oc_everywhere
            ):
                logging.error(f"Unsupported custom model: {requested_model}")
                sys.exit(1)
            self.constants.custom_model = requested_model
            defaults.GenerateDefaults(
                requested_model,
                False,
                self.constants,
                ignore_settings_file=True,
            )

        if getattr(self.args, "target_vol", None):
            self.constants.target_vol = self.args.target_vol
        elif getattr(self.args, "unpatch_target_vol", None):
            self.constants.target_vol = self.args.unpatch_target_vol


    def _parse_arguments(self) -> None:
        """
        Parses arguments passed to the patcher
        """

        if self.args is None:
            self._ocpbp_tui_handler()
            return

        if self.args.validate:
            self._validation_handler()
            return

        if (
            self.args.ocpbp
            or self.args.target_vol
            or self.args.unpatch_target_vol
            or self.args.sync_esp
            or self.args.offline_assets_dir
        ):
            self._configure_ocpbp_context()

        if self.args.ocpbp:
            self._ocpbp_tui_handler()
            return

        if self.args.target_vol:
            self._target_patch_handler()
            if self.args.sync_esp:
                self._sync_esp_handler()
            return

        if self.args.unpatch_target_vol:
            self._target_unpatch_handler()
            if self.args.sync_esp:
                self._sync_esp_handler()
            return

        if self.args.sync_esp:
            self._sync_esp_handler()
            return

        if self.args.build:
            self._build_handler()
            return

        if self.args.patch_sys_vol:
            self._sys_patch_handler()
            return

        if self.args.unpatch_sys_vol:
            self._sys_unpatch_handler()
            return

        if self.args.prepare_for_update:
            self._prepare_for_update_handler()
            return

        if self.args.cache_os:
            self._cache_os_handler()
            return

        if self.args.auto_patch:
            self._sys_patch_auto_handler()
            return


    def _validation_handler(self) -> None:
        """
        Enter validation mode
        """
        logging.info("Set Validation Mode")
        validation.PatcherValidation(self.constants)


    def _ocpbp_tui_handler(self) -> None:
        """
        Launch interactive Recovery Terminal UI without GUI dependencies
        """
        from ..ocpbp import tui

        assets_dir = getattr(self.args, "offline_assets_dir", None) if self.args else None
        ui = tui.OCPBPTerminalUI(self.constants, offline_assets_dir=assets_dir)
        exit_code = ui.run()
        if exit_code:
            sys.exit(exit_code)


    def _target_patch_handler(self) -> None:
        """
        Start offline root volume patching for --target_vol
        """
        target_vol = self.args.target_vol
        model = self.constants.custom_model or self.constants.computer.real_model
        interactive = bool(
            getattr(self.args, "interactive_unlock", False)
            or (sys.stdin is not None and sys.stdin.isatty())
        )
        logging.info(f"Starting offline OCPBP target volume patching for: {target_vol}")
        patcher = sys_patch.PatchSysVolume(
            model,
            self.constants,
            None,
            target_vol=target_vol,
            offline_assets_dir=self.constants.offline_assets_dir,
            passphrase=getattr(self.args, "passphrase", None),
            interactive_unlock=interactive,
        )
        patcher.start_patch()
        if patcher.patch_set_dictionary != {} and not self.constants.root_patcher_succeeded:
            logging.error(f"Offline root volume patching failed for target: {target_vol}")
            sys.exit(1)


    def _target_unpatch_handler(self) -> None:
        """
        Start offline root volume unpatching for --unpatch_target_vol
        """
        target_vol = self.args.unpatch_target_vol
        model = self.constants.custom_model or self.constants.computer.real_model
        interactive = bool(
            getattr(self.args, "interactive_unlock", False)
            or (sys.stdin is not None and sys.stdin.isatty())
        )
        logging.info(f"Starting offline OCPBP target volume unpatching for: {target_vol}")
        patcher = sys_patch.PatchSysVolume(
            model,
            self.constants,
            None,
            target_vol=target_vol,
            offline_assets_dir=self.constants.offline_assets_dir,
            passphrase=getattr(self.args, "passphrase", None),
            interactive_unlock=interactive,
        )
        patcher.start_unpatch()
        if not self.constants.root_patcher_succeeded:
            logging.error(f"Offline root volume unpatching failed for target: {target_vol}")
            sys.exit(1)


    def _sync_esp_handler(self) -> None:
        """
        Rebuild and install root-patch-ready OpenCore onto an internal ESP
        """
        from ..ocpbp import esp_sync
        from ..sys_patch.mount import mount

        target_group = None
        target_ref = getattr(self.args, "target_vol", None) or getattr(self.args, "unpatch_target_vol", None)
        if target_ref:
            try:
                target_group = mount.APFSVolumeResolver().resolve_target(target_ref)
            except Exception as exc:
                logging.warning(f"Could not resolve target group for ESP prioritization: {exc}")

        manager = esp_sync.ESPSyncManager(self.constants)
        result = manager.sync_internal_esp(
            esp_partition=getattr(self.args, "esp_disk", None),
            target_group=target_group,
            model=self.constants.custom_model,
            mode="rebuild",
        )
        logging.info(f"Internal ESP sync completed: {result}")


    def _sys_patch_handler(self) -> None:
        """
        Start root volume patching
        """

        logging.info("Set System Volume patching")
        if "Library/InstallerSandboxes/" in str(self.constants.payload_path):
            logging.info("- Running from Installer Sandbox, blocking OS updaters")
            thread = threading.Thread(target=sys_patch.PatchSysVolume(self.constants.custom_model or self.constants.computer.real_model, self.constants, None).start_patch)
            thread.start()
            while thread.is_alive():
                utilities.block_os_updaters()
                time.sleep(1)
        else:
            sys_patch.PatchSysVolume(self.constants.custom_model or self.constants.computer.real_model, self.constants, None).start_patch()


    def _sys_unpatch_handler(self) -> None:
        """
        Start root volume unpatching
        """
        logging.info("Set System Volume unpatching")
        sys_patch.PatchSysVolume(self.constants.custom_model or self.constants.computer.real_model, self.constants, None).start_unpatch()


    def _sys_patch_auto_handler(self) -> None:
        """
        Start root volume auto patching
        """

        logging.info("Set Auto patching")
        from ..sys_patch.auto_patcher.start import StartAutomaticPatching
        StartAutomaticPatching(self.constants).start_auto_patch()


    def _prepare_for_update_handler(self) -> None:
        """
        Prepare host for macOS update
        """
        logging.info("Preparing host for macOS update")

        os_data = utilities.fetch_staged_update(variant="Update")
        if os_data[0] is None:
            logging.info("No update staged, skipping")
            return

        os_version = os_data[0]
        os_build   = os_data[1]

        logging.info(f"Preparing for update to {os_version} ({os_build})")

        self._clean_le_handler()


    def _cache_os_handler(self) -> None:
        """
        Fetch KDK for incoming OS
        """
        results = subprocess.run(["/bin/ps", "-ax"], stdout=subprocess.PIPE)
        if results.stdout.decode("utf-8").count("OpenCore-Patcher --cache_os") > 1:
            logging.info("Another instance of OS caching is running, exiting")
            return

        from ..wx_gui import gui_entry
        gui_entry.EntryPoint(self.constants).start(entry=gui_entry.SupportedEntryPoints.OS_CACHE)


    def _clean_le_handler(self) -> None:
        """
        Clean /Library/Extensions of problematic kexts
        Note macOS Ventura and older do this automatically
        """

        if self.constants.detected_os < os_data.os_data.sonoma:
            return

        logging.info("Cleaning /Library/Extensions")

        for kext in Path("/Library/Extensions").glob("*.kext"):
            if not Path(f"{kext}/Contents/Info.plist").exists():
                continue
            try:
                kext_plist = plistlib.load(open(f"{kext}/Contents/Info.plist", "rb"))
            except Exception as e:
                logging.info(f"  - Failed to load plist for {kext.name}: {e}")
                continue
            if "GPUCompanionBundles" not in kext_plist:
                continue
            logging.info(f"  - Removing {kext.name}")
            subprocess_wrapper.run_as_root(["/bin/rm", "-rf", kext])


    def _build_handler(self) -> None:
        """
        Start config building process
        """
        logging.info("Set OpenCore Build")

        if self.args.model:
            if self.args.model:
                logging.info(f"- Using custom model: {self.args.model}")
                self.constants.custom_model = self.args.model
                defaults.GenerateDefaults(self.constants.custom_model, False, self.constants)
            elif self.constants.computer.real_model not in model_array.SupportedSMBIOS and self.constants.allow_oc_everywhere is False:
                logging.info(
                    """Your model is not supported by this patcher for running unsupported OSes!"

If you plan to create the USB for another machine, please select the "Change Model" option in the menu."""
                )
                sys.exit(1)
            else:
                logging.info(f"- Using detected model: {self.constants.computer.real_model}")
                defaults.GenerateDefaults(self.constants.custom_model, True, self.constants)

        if self.args.verbose:
            logging.info("- Set verbose configuration")
            self.constants.verbose_debug = True
        else:
            self.constants.verbose_debug = False  # Override Defaults detected

        if self.args.debug_oc:
            logging.info("- Set OpenCore DEBUG configuration")
            self.constants.opencore_debug = True

        if self.args.debug_kext:
            logging.info("- Set kext DEBUG configuration")
            self.constants.kext_debug = True

        if self.args.hide_picker:
            logging.info("- Set HidePicker configuration")
            self.constants.showpicker = False

        if self.args.disable_sip:
            logging.info("- Set Disable SIP configuration")
            self.constants.sip_status = False
        else:
            self.constants.sip_status = True  # Override Defaults detected

        if self.args.disable_smb:
            logging.info("- Set Disable SecureBootModel configuration")
            self.constants.secure_status = False
        else:
            self.constants.secure_status = True  # Override Defaults detected

        if self.args.vault:
            logging.info("- Set Vault configuration")
            self.constants.vault = True

        if self.args.firewire:
            logging.info("- Set FireWire Boot configuration")
            self.constants.firewire_boot = True

        if self.args.nvme:
            logging.info("- Set NVMe Boot configuration")
            self.constants.nvme_boot = True

        if self.args.wlan:
            logging.info("- Set Wake on WLAN configuration")
            self.constants.enable_wake_on_wlan = True

        if self.args.disable_tb:
            logging.info("- Set Disable Thunderbolt configuration")
            self.constants.disable_tb = True

        if self.args.force_surplus:
            logging.info("- Forcing SurPlus override configuration")
            self.constants.force_surplus = True

        if self.args.moderate_smbios:
            logging.info("- Set Moderate SMBIOS Patching configuration")
            self.constants.serial_settings = "Moderate"

        if self.args.smbios_spoof:
            if self.args.smbios_spoof == "Minimal":
                self.constants.serial_settings = "Minimal"
            elif self.args.smbios_spoof == "Moderate":
                self.constants.serial_settings = "Moderate"
            elif self.args.smbios_spoof == "Advanced":
                self.constants.serial_settings = "Advanced"
            else:
                logging.info(f"- Unknown SMBIOS arg passed: {self.args.smbios_spoof}")

        if self.args.support_all:
            logging.info("- Building for natively supported model")
            self.constants.allow_oc_everywhere = True
            self.constants.serial_settings = "None"

        build.BuildOpenCore(self.constants.custom_model or self.constants.computer.real_model, self.constants)

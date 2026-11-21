"""
application_entry.py: Project entry point
"""

import os
import sys
import time
import logging
import threading

from pathlib import Path

from . import constants

from .detections import (
    device_probe,
    os_probe
)
from .support import (
    utilities,
    defaults,
    arguments,
    reroute_payloads,
    commit_info,
    logging_handler,
    analytics_handler
)


class OpenCoreLegacyPatcher:
    """
    Initial entry point for starting OpenCore Legacy Patcher
    """

    def __init__(self, headless: bool = False) -> None:
        self.headless: bool = headless
        self.constants: constants.Constants = constants.Constants()
        self.constants.ocpbp_cli_mode = headless

        logging_handler.InitializeLoggingSupport(self.constants)

        cli_args = utilities.check_cli_args()
        self._generate_base_data(cli_args)

        if cli_args is None and not self.headless:
            from .wx_gui import gui_entry
            gui_entry.EntryPoint(self.constants).start()


    def _fix_cwd(self) -> None:
        """
        In some extreme scenarios, our current working directory may disappear
        """
        _test_dir = None
        try:
            _test_dir = Path.cwd()
            logging.info(f"Current working directory: {_test_dir}")
        except FileNotFoundError:
            _test_dir = Path(__file__).parent.parent.resolve()
            os.chdir(_test_dir)
            logging.warning(f"Current working directory was invalid, switched to: {_test_dir}")


    def _generate_base_data(self, cli_args=None) -> None:
        """
        Generate base data required for the patcher to run
        """

        is_ocpbp_invocation = bool(
            self.headless
            or (
                cli_args is not None
                and (
                    cli_args.ocpbp
                    or cli_args.target_vol
                    or cli_args.unpatch_target_vol
                    or cli_args.sync_esp
                )
            )
        )
        self.constants.wxpython_variant = not is_ocpbp_invocation

        # Ensure we live after parent process dies (ie. LaunchAgent)
        try:
            os.setpgrp()
        except OSError:
            pass

        # Generate OS data
        os_data = os_probe.OSProbe()
        self.constants.detected_os = os_data.detect_kernel_major()
        self.constants.detected_os_minor = os_data.detect_kernel_minor()
        self.constants.detected_os_build = os_data.detect_os_build()
        self.constants.detected_os_version = os_data.detect_os_version()

        # Generate computer data
        self.constants.computer = device_probe.Computer.probe()
        self.computer = self.constants.computer
        self.constants.booted_oc_disk = utilities.find_disk_off_uuid(utilities.clean_device_path(self.computer.opencore_path))
        if self.constants.computer.firmware_vendor:
            if self.constants.computer.firmware_vendor != "Apple":
                self.constants.host_is_hackintosh = True

        # Generate environment data
        self.constants.recovery_status = utilities.check_recovery()
        utilities.disable_cls()
        self._fix_cwd()

        # Generate binary data
        launcher_script = None
        launcher_binary = sys.executable
        if "python" in launcher_binary:
            # We're running from source
            launcher_script =  __file__
            if "main.py" in launcher_script:
                launcher_script = launcher_script.replace("/resources/main.py", "/OpenCore-Patcher-GUI.command")
        self.constants.launcher_binary = launcher_binary
        self.constants.launcher_script = launcher_script

        # Initialize working directory
        self.constants.unpack_thread = threading.Thread(target=reroute_payloads.RoutePayloadDiskImage, args=(self.constants,))
        self.constants.unpack_thread.start()

        # Generate commit info
        self.constants.commit_info = commit_info.ParseCommitInfo(self.constants.launcher_binary).generate_commit_info()
        if self.constants.commit_info[0] not in ["Running from source", "Built from source"]:
            ref = self.constants.commit_info[0]
            if ref.startswith("refs/tags/"):
                tag = ref.removeprefix("refs/tags/")
                self.constants.installer_pkg_url = f"{self.constants.repo_link}/releases/download/{tag}/AutoPkg-Assets.pkg"
            elif ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
                self.constants.installer_pkg_url_nightly = self.constants.installer_pkg_url_nightly.replace("/main/", f"/{branch}/")

        # Generate defaults
        defaults.GenerateDefaults(self.computer.real_model, True, self.constants)
        if not is_ocpbp_invocation:
            threading.Thread(target=analytics_handler.Analytics(self.constants).send_analytics).start()

        if cli_args is None and not self.headless:
            self.constants.cli_mode = False
            return

        logging.info("Detected arguments, switching to CLI mode")
        self.constants.gui_mode = True  # Assumes no user interaction is required

        ignore_args = ["--auto_patch", "--gui_patch", "--gui_unpatch", "--update_installed"]
        if (
            not any(x in sys.argv for x in ignore_args)
            and not is_ocpbp_invocation
            and self.constants.launcher_script
        ):
            self.constants.current_path = Path.cwd()
        ignore_args = ignore_args.pop(0)

        if not any(x in sys.argv for x in ignore_args):
            while self.constants.unpack_thread.is_alive():
                time.sleep(self.constants.thread_sleep_interval)

        arguments.arguments(self.constants, parsed_args=cli_args)

def main(headless: bool = False):
    """
    Main entry point
    """
    OpenCoreLegacyPatcher(headless=headless)
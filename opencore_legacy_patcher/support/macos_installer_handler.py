"""
macos_installer_handler.py: Handler for local macOS installers
"""

import logging
import math
import os
import plistlib
import re
import shlex
import subprocess
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .. import constants
from ..datasets import os_data

from . import (
    network_handler,
    utilities,
    subprocess_wrapper
)

from ..volume import (
    can_copy_on_write,
    generate_copy_arguments
)


APPLICATION_SEARCH_PATH:  str = "/Applications"

DEFAULT_MIN_INSTALLER_PARTITION_BYTES: int = 17_000_000_000
DEFAULT_MIN_OCPBP_PARTITION_BYTES: int = 4_000_000_000
DEFAULT_INSTALLER_HEADROOM_BYTES: int = 2_500_000_000
EFI_PARTITION_OVERHEAD_BYTES: int = 250_000_000

tmp_dir = tempfile.TemporaryDirectory()


def measure_installer_app_size(installer_path: Optional[Union[str, Path]]) -> int:
    """
    Calculate total apparent file size (bytes) inside a macOS installer .app bundle.
    """
    if not installer_path:
        return 0
    root = Path(installer_path)
    if not root.exists():
        return 0

    total_bytes = 0
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total_bytes += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total_bytes


def compute_ocpbp_usb_layout(
    disk_size_bytes: int,
    installer_path: Optional[Union[str, Path]] = None,
    min_ocpbp_bytes: int = DEFAULT_MIN_OCPBP_PARTITION_BYTES,
) -> dict:
    """
    Calculate whether a USB disk has sufficient capacity for the 3-partition OCPBP GPT layout:
      1. EFI (GPT ESP)
      2. OCLP-Installer (JHFS+, dynamically sized for installer + headroom)
      3. OCPBP (JHFS+, remainder for headless ocpbp binary + offline assets)
    """
    app_bytes = measure_installer_app_size(installer_path)
    effective_app_bytes = app_bytes if app_bytes > 0 else 14_000_000_000
    installer_needed = max(
        effective_app_bytes + DEFAULT_INSTALLER_HEADROOM_BYTES,
        DEFAULT_MIN_INSTALLER_PARTITION_BYTES,
    )
    installer_partition_gb = int(math.ceil(installer_needed / 1_000_000_000))
    allocated_installer_bytes = installer_partition_gb * 1_000_000_000
    required_total_bytes = allocated_installer_bytes + EFI_PARTITION_OVERHEAD_BYTES + min_ocpbp_bytes

    total_disk_bytes = int(disk_size_bytes)
    remaining_ocpbp_bytes = max(total_disk_bytes - allocated_installer_bytes - EFI_PARTITION_OVERHEAD_BYTES, 0)
    fits = total_disk_bytes >= required_total_bytes

    reason = ""
    if not fits:
        reason = (
            f"Needs at least {utilities.human_fmt(required_total_bytes)} "
            f"(disk is {utilities.human_fmt(total_disk_bytes)})"
        )

    return {
        "fits": fits,
        "disk_size_bytes": total_disk_bytes,
        "installer_app_bytes": app_bytes,
        "installer_partition_gb": installer_partition_gb,
        "installer_size_arg": f"{installer_partition_gb}G",
        "ocpbp_estimated_bytes": remaining_ocpbp_bytes,
        "required_total_bytes": required_total_bytes,
        "reason": reason,
    }


class InstallerCreation():

    def __init__(self) -> None:
        pass


    def install_macOS_installer(self, download_path: str) -> bool:
        """
        Installs InstallAssistant.pkg

        Parameters:
            download_path (str): Path to InstallAssistant.pkg

        Returns:
            bool: True if successful, False otherwise
        """

        logging.info("Extracting macOS installer from InstallAssistant.pkg")
        result = subprocess_wrapper.run_as_root(["/usr/sbin/installer", "-pkg", f"{Path(download_path)}/InstallAssistant.pkg", "-target", "/"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            logging.info("Failed to install InstallAssistant")
            subprocess_wrapper.log(result)
            return False

        logging.info("InstallAssistant installed")
        return True


    def generate_installer_creation_script(
        self,
        tmp_location: str,
        installer_path: str,
        disk: str,
        ocpbp_enabled: bool = False,
        installer_partition_size: Optional[str] = None,
    ) -> bool:
        """
        Creates installer.sh to be piped to OCLP-Helper and run as admin

        Script includes:
        - Format provided disk as HFS+ GPT (or 3-partition GPT with OCPBP volume)
        - Run createinstallmedia on provided disk

        Implementing this into a single installer.sh script allows us to only call
        OCLP-Helper once to avoid nagging the user about permissions

        Parameters:
            tmp_location (str): Path to temporary directory
            installer_path (str): Path to InstallAssistant.pkg
            disk (str): Disk to install to
            ocpbp_enabled (bool): Pre-partition GPT disk with EFI + OCLP-Installer + OCPBP
            installer_partition_size (Optional[str]): Explicit size for OCLP-Installer partition (e.g. '19G')

        Returns:
            bool: True if successful, False otherwise
        """

        additional_args = ""
        script_location = Path(tmp_location) / Path("Installer.sh")
        original_app_size = measure_installer_app_size(installer_path)

        # Due to a bug in createinstallmedia, running from '/Applications' may sometimes error:
        #   'Failed to extract AssetData/boot/Firmware/Manifests/InstallerBoot/*'
        # This affects native Macs as well even when manually invoking createinstallmedia

        # To resolve, we'll copy into our temp directory and run from there

        # Create a new tmp directory
        # Our current one is a disk image, thus CoW will not work
        global tmp_dir
        ia_tmp = tmp_dir.name

        logging.info(f"Creating temporary directory at {ia_tmp}")
        # Delete all files in tmp_dir
        for file in Path(ia_tmp).glob("*"):
            subprocess.run(["/bin/rm", "-rf", str(file)])

        # Copy installer to tmp
        if can_copy_on_write(installer_path, ia_tmp) is False:
            # Ensure we have enough space for the duplication when CoW is not supported
            space_available = utilities.get_free_space()
            space_needed = sum(f.stat().st_size for f in Path(installer_path).rglob("*") if f.is_file() and not f.is_symlink())
            if space_available < space_needed:
                logging.info("Not enough free space to create installer.sh")
                logging.info(f"{utilities.human_fmt(space_available)} available, {utilities.human_fmt(space_needed)} required")
                return False

        subprocess.run(generate_copy_arguments(installer_path, ia_tmp))

        # Adjust installer_path to point to the copied installer
        installer_path = Path(ia_tmp) / Path(Path(installer_path).name)
        if not Path(installer_path).exists():
            logging.info(f"Failed to copy installer to {ia_tmp}")
            return False

        # Verify code signature before executing
        createinstallmedia_path = str(Path(installer_path) / Path("Contents/Resources/createinstallmedia"))
        if subprocess.run(["/usr/bin/codesign", "-v", "-R=anchor apple", createinstallmedia_path]).returncode != 0:
            logging.info(f"Installer has broken code signature")
            return False

        plist_path = Path(installer_path) / Path("Contents/Info.plist")
        if plist_path.exists():
            with plist_path.open("rb") as plist_file:
                plist = plistlib.load(plist_file)
            if "DTPlatformVersion" in plist:
                platform_version = plist["DTPlatformVersion"]
                platform_version = platform_version.split(".")[0]
                if platform_version[0] == "10":
                    if int(platform_version[1]) < 13:
                        additional_args = f" --applicationpath '{installer_path}'"

        if script_location.exists():
            script_location.unlink()
        script_location.touch()

        with script_location.open("w") as script:
            if ocpbp_enabled:
                size_arg = installer_partition_size
                if not size_arg:
                    app_bytes = original_app_size or measure_installer_app_size(installer_path) or 14_000_000_000
                    size_gb = int(math.ceil(max(app_bytes + DEFAULT_INSTALLER_HEADROOM_BYTES, DEFAULT_MIN_INSTALLER_PARTITION_BYTES) / 1_000_000_000))
                    size_arg = f"{size_gb}G"
                partition_cmd = (
                    f"/usr/sbin/diskutil partitionDisk {shlex.quote(disk)} GPT "
                    f"JHFS+ OCLP-Installer {shlex.quote(size_arg)} JHFS+ OCPBP R"
                )
                script.write(
                    "#!/bin/bash\n"
                    f"if {partition_cmd}; then\n"
                    f"    {shlex.quote(createinstallmedia_path)} --volume /Volumes/OCLP-Installer --nointeraction{additional_args}\n"
                    "fi\n"
                )
            else:
                script.write(f'''#!/bin/bash
erase_disk='/usr/sbin/diskutil eraseDisk HFS+ OCLP-Installer {disk}'
if $erase_disk; then
    "{createinstallmedia_path}" --volume /Volumes/OCLP-Installer --nointeraction{additional_args}
fi
            ''')
        if Path(script_location).exists():
            return True
        return False


    @staticmethod
    def locate_ocpbp_binary(global_constants: constants.Constants) -> Optional[Path]:
        """
        Locate the compiled standalone headless `ocpbp` executable when available.
        """
        candidates: list[Path] = []
        if getattr(global_constants, "launcher_binary", None):
            launcher_path = Path(global_constants.launcher_binary)
            candidates.append(launcher_path.parent / "ocpbp")
            candidates.append(launcher_path.parent.parent / "Resources" / "ocpbp")
        if getattr(global_constants, "original_path", None):
            orig = Path(global_constants.original_path)
            candidates.append(orig / "ocpbp")
            candidates.append(orig / "dist" / "ocpbp")
        candidates.append(Path.cwd() / "dist" / "ocpbp")

        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None


    @staticmethod
    def _write_ocpbp_launcher(ocpbp_mount: Path) -> Path:
        launcher = ocpbp_mount / "ocpbp.command"
        launcher.write_text(
            "#!/bin/bash\n"
            'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'if [ -x "$SCRIPT_DIR/ocpbp" ]; then\n'
            '    exec "$SCRIPT_DIR/ocpbp" --ocpbp --offline_assets_dir "$SCRIPT_DIR" "$@"\n'
            "fi\n"
            'echo "Standalone ocpbp binary was not bundled at $SCRIPT_DIR/ocpbp."\n'
            'echo "Build it with: python3 Build-Project.command --prepare-ocpbp"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        os.chmod(launcher, 0o755)
        return launcher


    def populate_ocpbp_volume(
        self,
        global_constants: constants.Constants,
        ocpbp_mount_path: Union[str, Path],
        installer_os_version: str = "",
        installer_build: str = "",
        target_model: str = "",
        download_if_missing: bool = True,
    ) -> dict:
        """
        Populate the companion /Volumes/OCPBP payload volume without duplicating
        assets into /Library/Packages on the OCLP-Installer partition.
        """
        dest = Path(ocpbp_mount_path)
        dest.mkdir(parents=True, exist_ok=True)
        populated: dict[str, Optional[str]] = {
            "mount_path": str(dest),
            "ocpbp_binary": None,
            "launcher": None,
            "autopkg_assets": None,
            "universal_binaries": None,
            "manifest": None,
        }

        ocpbp_bin = self.locate_ocpbp_binary(global_constants)
        if ocpbp_bin and ocpbp_bin.is_file():
            dst_bin = dest / "ocpbp"
            copy_res = subprocess.run(generate_copy_arguments(str(ocpbp_bin), str(dst_bin)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if copy_res.returncode != 0:
                subprocess_wrapper.log(copy_res)
                raise RuntimeError(f"Failed to copy ocpbp binary to {dst_bin}")
            os.chmod(dst_bin, 0o755)
            populated["ocpbp_binary"] = str(dst_bin)
        else:
            logging.warning("Compiled headless 'ocpbp' binary not found; only launcher script created.")

        launcher_path = self._write_ocpbp_launcher(dest)
        populated["launcher"] = str(launcher_path)

        autopkg_src = Path(global_constants.installer_pkg_path)
        if autopkg_src.is_file():
            autopkg_dst = dest / "AutoPkg-Assets.pkg"
            copy_res = subprocess.run(generate_copy_arguments(str(autopkg_src), str(autopkg_dst)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if copy_res.returncode != 0:
                subprocess_wrapper.log(copy_res)
                raise RuntimeError(f"Failed to copy AutoPkg-Assets.pkg to {autopkg_dst}")
            populated["autopkg_assets"] = str(autopkg_dst)

        ub_dst = dest / "Universal-Binaries.dmg"
        ub_candidates = [
            Path(global_constants.payload_local_binaries_root_path_dmg),
            Path(global_constants.original_path) / "Universal-Binaries.dmg",
        ]
        ub_src = next((p for p in ub_candidates if p.is_file()), None)
        if ub_src:
            copy_res = subprocess.run(generate_copy_arguments(str(ub_src), str(ub_dst)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if copy_res.returncode != 0:
                subprocess_wrapper.log(copy_res)
                raise RuntimeError(f"Failed to copy Universal-Binaries.dmg to {ub_dst}")
        elif download_if_missing:
            ub_url = (
                f"{global_constants.url_patcher_support_pkg}"
                f"{global_constants.patcher_support_pkg_version}/Universal-Binaries.dmg"
            )
            dl = network_handler.DownloadObject(ub_url, ub_dst)
            dl.download(spawn_thread=False)

        if ub_dst.is_file():
            populated["universal_binaries"] = str(ub_dst)

        manifest_path = dest / ".ocpbp-manifest.plist"
        manifest_data = {
            "OCPBPVersion": global_constants.ocpbp_version,
            "PatcherVersion": global_constants.patcher_version,
            "InstallerOSVersion": installer_os_version,
            "InstallerBuild": installer_build,
            "TargetModel": target_model,
            "CreatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with manifest_path.open("wb") as handle:
            plistlib.dump(manifest_data, handle)
        populated["manifest"] = str(manifest_path)

        return populated


    def list_disk_to_format(self) -> dict:
        """
        List applicable disks for macOS installer creation
        Only lists disks that are:
        - 14GB or larger
        - External

        Current limitations:
        - Does not support PCIe based SD cards readers

        Returns:
            dict: Dictionary of disks
        """

        all_disks:  dict = {}
        list_disks: dict = {}

        # TODO: AllDisksAndPartitions is not supported in Snow Leopard and older
        try:
            # High Sierra and newer
            disks = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "list", "-plist", "physical"], stdout=subprocess.PIPE).stdout.decode().strip().encode())
        except ValueError:
            # Sierra and older
            disks = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "list", "-plist"], stdout=subprocess.PIPE).stdout.decode().strip().encode())

        for disk in disks["AllDisksAndPartitions"]:
            try:
                disk_info = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "info", "-plist", disk["DeviceIdentifier"]], stdout=subprocess.PIPE).stdout.decode().strip().encode())
            except:
                # Chinesium USB can have garbage data in MediaName
                diskutil_output = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", disk["DeviceIdentifier"]], stdout=subprocess.PIPE).stdout.decode().strip()
                ungarbafied_output = re.sub(r'(<key>MediaName</key>\s*<string>).*?(</string>)', r'\1\2', diskutil_output).encode()
                disk_info = plistlib.loads(ungarbafied_output)
            try:
                all_disks[disk["DeviceIdentifier"]] = {"identifier": disk_info["DeviceNode"], "name": disk_info.get("MediaName", "Disk"), "size": disk_info["TotalSize"], "removable": disk_info["Internal"], "partitions": {}}
            except KeyError:
                # Avoid crashing with CDs installed
                continue

        for disk in all_disks:
            # Strip disks that are under 14GB (15,032,385,536 bytes)
            # createinstallmedia isn't great at detecting if a disk has enough space
            if not any(all_disks[disk]['size'] > 15032385536 for partition in all_disks[disk]):
                continue
            # Strip internal disks as well (avoid user formatting their SSD/HDD)
            # Ensure user doesn't format their boot drive
            if not any(all_disks[disk]['removable'] is False for partition in all_disks[disk]):
                continue

            list_disks.update({
                disk: {
                    "identifier": all_disks[disk]["identifier"],
                    "name": all_disks[disk]["name"],
                    "size": all_disks[disk]["size"],
                }
            })

        return list_disks


class LocalInstallerCatalog:
    """
    Finds all macOS installers on the local machine.
    """

    def __init__(self) -> None:
        self.available_apps: dict = self._list_local_macOS_installers()


    def _list_local_macOS_installers(self) -> dict:
        """
        Searches for macOS installers in /Applications

        Returns:
            dict: A dictionary of macOS installers found on the local machine.

            Example:
                "Install macOS Big Sur Beta.app": {
                    "Short Name": "Big Sur Beta",
                    "Version": "11.0",
                    "Build": "20A5343i",
                    "Path": "/Applications/Install macOS Big Sur Beta.app",
                },
                etc...
        """

        application_list: dict = {}

        for application in Path(APPLICATION_SEARCH_PATH).iterdir():
            # Certain Microsoft Applications have strange permissions disabling us from reading them
            try:
                if not (Path(APPLICATION_SEARCH_PATH) / Path(application) / Path("Contents/Resources/createinstallmedia")).exists():
                    continue

                if not (Path(APPLICATION_SEARCH_PATH) / Path(application) / Path("Contents/Info.plist")).exists():
                    continue
            except PermissionError:
                continue

            try:
                application_info_plist = plistlib.load((Path(APPLICATION_SEARCH_PATH) / Path(application) / Path("Contents/Info.plist")).open("rb"))
            except (PermissionError, TypeError, plistlib.InvalidFileException):
                continue

            if "DTPlatformVersion" not in application_info_plist:
                continue
            if "CFBundleDisplayName" not in application_info_plist:
                continue

            app_version:  str = application_info_plist["DTPlatformVersion"]
            clean_name:   str = application_info_plist["CFBundleDisplayName"]
            app_sdk:      str = application_info_plist["DTSDKBuild"] if "DTSDKBuild" in application_info_plist else "Unknown"
            min_required: str = application_info_plist["LSMinimumSystemVersion"] if "LSMinimumSystemVersion" in application_info_plist else "Unknown"

            kernel:       int = 0
            try:
                kernel = int(app_sdk[:2])
            except ValueError:
                pass

            min_required = os_data.os_conversion.os_to_kernel(min_required) if min_required != "Unknown" else 0

            if min_required == os_data.os_data.sierra and kernel == os_data.os_data.ventura:
                # Ventura's installer requires El Capitan minimum
                # Ref: https://github.com/dortania/OpenCore-Legacy-Patcher/discussions/1038
                min_required = os_data.os_data.el_capitan

            # app_version can sometimes report GM instead of the actual version
            # This is a workaround to get the actual version
            if app_version.startswith("GM"):
                if kernel == 0:
                    app_version = "Unknown"
                else:
                    app_version = os_data.os_conversion.kernel_to_os(kernel)

            # Check if App Version is High Sierra or newer
            if kernel < os_data.os_data.high_sierra:
                continue

            results = self._parse_sharedsupport_version(Path(APPLICATION_SEARCH_PATH) / Path(application)/ Path("Contents/SharedSupport/SharedSupport.dmg"))
            if results[0] is not None:
                app_sdk = results[0]
            if results[1] is not None:
                app_version = results[1]

            application_list.update({
                application: {
                    "Short Name": clean_name,
                    "Version": app_version,
                    "Build": app_sdk,
                    "Path": application,
                    "Minimum Host OS": min_required,
                    "OS": kernel
                }
            })

        # Sort Applications by version
        application_list = {k: v for k, v in sorted(application_list.items(), key=lambda item: item[1]["Version"])}
        return application_list


    def _parse_sharedsupport_version(self, sharedsupport_path: Path) -> tuple:
        """
        Determine true version of macOS installer by parsing SharedSupport.dmg
        This is required due to Info.plist reporting the application version, not the OS version

        Parameters:
            sharedsupport_path (Path): Path to SharedSupport.dmg

        Returns:
            tuple: Tuple containing the build and OS version
        """

        detected_build: str = None
        detected_os:    str = None

        if not sharedsupport_path.exists():
            return (detected_build, detected_os)

        if not sharedsupport_path.name.endswith(".dmg"):
            return (detected_build, detected_os)


        # Create temporary directory to extract SharedSupport.dmg to
        with tempfile.TemporaryDirectory() as tmpdir:

            output = subprocess.run(
                [
                    "/usr/bin/hdiutil", "attach", "-noverify", sharedsupport_path,
                    "-mountpoint", tmpdir,
                    "-nobrowse",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            if output.returncode != 0:
                return (detected_build, detected_os)


            ss_info_files = [
                Path("SFR/com_apple_MobileAsset_SFRSoftwareUpdate/com_apple_MobileAsset_SFRSoftwareUpdate.xml"),
                Path("com_apple_MobileAsset_MacSoftwareUpdate/com_apple_MobileAsset_MacSoftwareUpdate.xml")
            ]

            for ss_info in ss_info_files:
                if not Path(tmpdir / ss_info).exists():
                    continue
                plist = plistlib.load((tmpdir / ss_info).open("rb"))
                if "Assets" in plist:
                    if "Build" in plist["Assets"][0]:
                        detected_build = plist["Assets"][0]["Build"]
                    if "OSVersion" in plist["Assets"][0]:
                        detected_os = plist["Assets"][0]["OSVersion"]

            # Unmount SharedSupport.dmg
            subprocess.run(["/usr/bin/hdiutil", "detach", tmpdir], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        return (detected_build, detected_os)
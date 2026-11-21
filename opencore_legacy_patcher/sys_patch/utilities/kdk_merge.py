
import logging
import os
import plistlib
import subprocess
import tempfile

from pathlib import Path
from typing import Optional, Union

from ... import constants

from ...datasets import os_data
from ...support import subprocess_wrapper, kdk_handler
from ...volume import generate_copy_arguments


class KernelDebugKitMerge:

    def __init__(
        self,
        global_constants: constants.Constants,
        mount_location: str,
        skip_root_kmutil_requirement: bool,
        target_data_mount: str = "",
        target_os_build: Optional[str] = None,
        target_os_version: Optional[str] = None,
        target_os_major: Optional[int] = None,
        offline_assets_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.constants: constants.Constants = global_constants
        self.mount_location = mount_location
        self.skip_root_kmutil_requirement = skip_root_kmutil_requirement
        self.target_data_mount = target_data_mount
        self.target_os_build = target_os_build or self.constants.detected_os_build
        self.target_os_version = target_os_version or self.constants.detected_os_version
        self.target_os_major = target_os_major if target_os_major is not None else self.constants.detected_os
        self.offline_assets_dir = offline_assets_dir or getattr(self.constants, "offline_assets_dir", None)
        self._hid_backup_path: Optional[Path] = None


    def _resolved_kdk_install_path(self) -> Optional[str]:
        if self.target_data_mount and str(self.target_data_mount) != "/":
            return str(Path(self.target_data_mount) / "Library/Developer/KDKs")
        return None


    def _resolved_target_volume(self) -> Optional[str]:
        if self.target_data_mount and str(self.target_data_mount) != "/":
            return str(self.target_data_mount)
        if self.mount_location and str(self.mount_location) not in ("", "/", "/System/Volumes/Update/mnt1"):
            return str(self.mount_location)
        return None


    def _matching_kdk_already_merged(self, kdk_path: str) -> bool:
        """
        Check whether the KDK is already merged with the root volume
        """
        root_base = Path(self.mount_location or "/")
        oclp_plist = root_base / "System/Library/CoreServices/OpenCore-Legacy-Patcher.plist"
        if not oclp_plist.exists():
            return False

        if not (root_base / Path("System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern")).exists():
            return False

        try:
            with open(oclp_plist, "rb") as f:
                oclp_plist_data = plistlib.load(f)
            if "Kernel Debug Kit Used" not in oclp_plist_data:
                return False
            if oclp_plist_data["Kernel Debug Kit Used"] == str(kdk_path):
                logging.info("- Matching KDK determined to already be merged, skipping")
                return True
        except Exception:
            pass

        return False


    def _backup_hid_cs(self) -> None:
        """
        Due to some IOHIDFamily oddities, we need to ensure their CodeSignature is retained
        """
        cs_path = Path(self.mount_location or "/") / Path("System/Library/Extensions/IOHIDFamily.kext/Contents/PlugIns/IOHIDEventDriver.kext/Contents/_CodeSignature")
        if not cs_path.exists():
            return

        if Path(self.constants.payload_path).exists() and os.access(self.constants.payload_path, os.W_OK):
            self._hid_backup_path = Path(self.constants.payload_path) / "IOHIDEventDriver_CodeSignature.bak"
        else:
            self._hid_backup_path = Path(tempfile.mkdtemp(prefix="ocpbp_hidcs_")) / "IOHIDEventDriver_CodeSignature.bak"

        logging.info("- Backing up IOHIDEventDriver CodeSignature")
        subprocess_wrapper.run_as_root(generate_copy_arguments(cs_path, str(self._hid_backup_path)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


    def _restore_hid_cs(self) -> None:
        """
        Restore IOHIDEventDriver CodeSignature
        """
        backup_path = self._hid_backup_path or (Path(self.constants.payload_path) / "IOHIDEventDriver_CodeSignature.bak")
        if not backup_path.exists():
            return

        logging.info("- Restoring IOHIDEventDriver CodeSignature")
        cs_path = Path(self.mount_location or "/") / Path("System/Library/Extensions/IOHIDFamily.kext/Contents/PlugIns/IOHIDEventDriver.kext/Contents/_CodeSignature")
        if not cs_path.exists():
            logging.info("  - CodeSignature folder missing, creating")
            subprocess_wrapper.run_as_root(["/bin/mkdir", "-p", cs_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        subprocess_wrapper.run_as_root(generate_copy_arguments(str(backup_path), cs_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(backup_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if self._hid_backup_path and self._hid_backup_path.parent.name.startswith("ocpbp_hidcs_"):
            try:
                self._hid_backup_path.parent.rmdir()
            except OSError:
                pass
        self._hid_backup_path = None


    def _merge_kdk(self, kdk_path: str) -> None:
        """
        Merge Kernel Debug Kit (KDK) with the root volume
        """
        logging.info(f"- Merging KDK with Root Volume: {Path(kdk_path).name}")
        subprocess_wrapper.run_as_root(
            # Only merge '/System/Library/Extensions'
            # 'Kernels' and 'KernelSupport' is wasted space for root patching (we don't care above dev kernels)
            ["/usr/bin/rsync", "-r", "-i", "-a", f"{kdk_path}/System/Library/Extensions/", f"{self.mount_location}/System/Library/Extensions"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        if not (Path(self.mount_location or "/") / Path("System/Library/Extensions/System.kext/PlugIns/Libkern.kext/Libkern")).exists():
            logging.info("- Failed to merge KDK with Root Volume")
            raise Exception("Failed to merge KDK with Root Volume")
        logging.info("- Successfully merged KDK with Root Volume")


    def merge(self, save_hid_cs: bool = False) -> str:
        """
        Merge the Kernel Debug Kit (KDK) with the root volume

        Returns KDK used
        """
        if self.skip_root_kmutil_requirement is True:
            return None
        if self.target_os_major < os_data.os_data.ventura:
            return None

        kdk_install_path = self._resolved_kdk_install_path()
        target_volume = self._resolved_target_volume()

        # If a KDK was pre-downloaded, install it
        if self.constants.kdk_download_path.exists():
            if kdk_handler.KernelDebugKitUtilities().install_kdk_dmg(
                self.constants.kdk_download_path,
                target_volume=target_volume,
                kdk_install_path=kdk_install_path,
            ) is False:
                logging.info("Failed to install KDK")
                raise Exception("Failed to install KDK")

        # Next, grab KDK information (ie. what's the latest KDK for this OS)
        kdk_obj = kdk_handler.KernelDebugKitObject(
            self.constants,
            self.target_os_build,
            self.target_os_version,
            kdk_install_path=kdk_install_path,
            offline_assets_dir=self.offline_assets_dir,
            target_volume=target_volume,
        )
        if kdk_obj.success is False:
            logging.info(f"Unable to get KDK info: {kdk_obj.error_msg}")
            raise Exception(f"Unable to get KDK info: {kdk_obj.error_msg}")

        # Install from offline asset if available and not yet extracted to target Data
        if kdk_obj.kdk_already_installed is False and kdk_obj.kdk_available_offline is True:
            asset_path = Path(kdk_obj.kdk_offline_asset_path)
            if kdk_obj.kdk_offline_asset_type == "pkg":
                installed = kdk_handler.KernelDebugKitUtilities().install_kdk_pkg(
                    asset_path,
                    target_volume=target_volume,
                    kdk_install_path=kdk_install_path,
                )
            else:
                installed = kdk_handler.KernelDebugKitUtilities().install_kdk_dmg(
                    asset_path,
                    target_volume=target_volume,
                    kdk_install_path=kdk_install_path,
                )
            if installed is False:
                logging.info(f"Failed to install offline KDK asset: {asset_path}")
                raise Exception(f"Failed to install offline KDK asset: {asset_path}")

            kdk_obj = kdk_handler.KernelDebugKitObject(
                self.constants,
                self.target_os_build,
                self.target_os_version,
                kdk_install_path=kdk_install_path,
                offline_assets_dir=self.offline_assets_dir,
                target_volume=target_volume,
            )
            if kdk_obj.success is False or kdk_obj.kdk_already_installed is False:
                logging.info(f"Offline KDK failed validation after extraction: {kdk_obj.error_msg}")
                raise Exception(f"Offline KDK failed validation after extraction: {kdk_obj.error_msg}")

        # If no KDK is installed, download and install it
        if kdk_obj.kdk_already_installed is False:
            kdk_download_obj = kdk_obj.retrieve_download()
            if not kdk_download_obj:
                logging.info(f"Could not retrieve KDK: {kdk_obj.error_msg}")
                raise Exception(f"Could not retrieve KDK: {kdk_obj.error_msg}")

            # Hold thread until download is complete
            kdk_download_obj.download(spawn_thread=False)

            if kdk_download_obj.download_complete is False:
                error_msg = kdk_download_obj.error_msg
                logging.info(f"Could not download KDK: {error_msg}")
                raise Exception(f"Could not download KDK: {error_msg}")

            if kdk_obj.validate_kdk_checksum() is False:
                logging.info(f"KDK checksum validation failed: {kdk_obj.error_msg}")
                raise Exception(f"KDK checksum validation failed: {kdk_obj.error_msg}")

            kdk_handler.KernelDebugKitUtilities().install_kdk_dmg(
                self.constants.kdk_download_path,
                target_volume=target_volume,
                kdk_install_path=kdk_install_path,
            )
            # re-init kdk_obj to get the new kdk_installed_path
            kdk_obj = kdk_handler.KernelDebugKitObject(
                self.constants,
                self.target_os_build,
                self.target_os_version,
                kdk_install_path=kdk_install_path,
                offline_assets_dir=self.offline_assets_dir,
                target_volume=target_volume,
            )
            if kdk_obj.success is False:
                logging.info(f"Unable to get KDK info: {kdk_obj.error_msg}")
                raise Exception(f"Unable to get KDK info: {kdk_obj.error_msg}")

            if kdk_obj.kdk_already_installed is False:
                # We shouldn't get here, but just in case
                logging.warning(f"KDK was not installed, but should have been: {kdk_obj.error_msg}")
                raise Exception(f"KDK was not installed, but should have been: {kdk_obj.error_msg}")


        kdk_path = Path(kdk_obj.kdk_installed_path) if kdk_obj.kdk_installed_path != "" else None
        if kdk_path is None:
            logging.info(f"- Unable to find Kernel Debug Kit")
            raise Exception("Unable to find Kernel Debug Kit")

        logging.info(f"- Found KDK at: {kdk_path}")

        if self._matching_kdk_already_merged(kdk_path):
            return kdk_path

        if save_hid_cs is True:
            self._backup_hid_cs()

        self._merge_kdk(kdk_path)

        if save_hid_cs is True:
            self._restore_hid_cs()

        return kdk_path
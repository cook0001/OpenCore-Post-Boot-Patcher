"""
sys_patch_helpers.py: Additional support functions for sys_patch.py
"""

import os
import shlex
import shutil
import logging
import plistlib
import tempfile
import subprocess

from typing import Optional, Union
from pathlib import Path
from datetime import datetime

from .. import constants

from ..datasets import os_data
from ..volume   import generate_copy_arguments

from ..support import (
    generate_smbios,
    subprocess_wrapper
)


class SysPatchHelpers:
    """
    Library of helper functions for sys_patch.py and related libraries
    """

    def __init__(self, global_constants: constants.Constants):
        self.constants: constants.Constants = global_constants
        self.last_generated_plist_path: Optional[Path] = None


    @staticmethod
    def target_scope_execute_command(
        process: str,
        target_data_mount: Optional[Union[str, Path]] = None,
        as_root: bool = True,
    ) -> Union[str, list[str]]:
        """
        Rewrite PatchType.EXECUTE command arguments so host paths like
        '/Library/...' or '/private/...' operate against `target_data_mount`
        when patching offline.
        """
        if not target_data_mount or str(target_data_mount) in ("", "/"):
            return process.split(" ") if as_root else process

        data_root = Path(target_data_mount)
        tokens = shlex.split(process)
        rewritten: list[str] = []
        for token in tokens:
            if token.startswith("/Library/") or token.startswith("/private/"):
                rewritten.append(str(data_root / token.lstrip("/")))
            else:
                rewritten.append(token)
        return rewritten


    def run_execute_command(
        self,
        process: str,
        as_root: bool = True,
        target_data_mount: Optional[Union[str, Path]] = None,
    ) -> None:
        is_offline = bool(target_data_mount and str(target_data_mount) not in ("", "/"))
        scoped = self.target_scope_execute_command(process, target_data_mount=target_data_mount, as_root=as_root)

        if is_offline and isinstance(scoped, list):
            # Ensure parent directory exists when running `defaults write <path> ...`
            if len(scoped) >= 3 and os.path.basename(scoped[0]) == "defaults" and scoped[1] == "write" and scoped[2].startswith("/"):
                domain_parent = Path(scoped[2]).parent
                if not domain_parent.exists():
                    subprocess_wrapper.run_as_root(["/bin/mkdir", "-p", str(domain_parent)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            if as_root:
                logging.info(f"- Running Process as Root:\n{' '.join(shlex.quote(x) for x in scoped)}")
                subprocess_wrapper.run_as_root_and_verify(scoped, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            else:
                logging.info(f"- Running Process:\n{' '.join(shlex.quote(x) for x in scoped)}")
                subprocess_wrapper.run_and_verify(scoped, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return

        if as_root:
            logging.info(f"- Running Process as Root:\n{process}")
            subprocess_wrapper.run_as_root_and_verify(scoped, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            logging.info(f"- Running Process:\n{process}")
            subprocess_wrapper.run_and_verify(scoped, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)


    def snb_board_id_patch(self, source_files_path: str):
        """
        Patch AppleIntelSNBGraphicsFB.kext to support unsupported Board IDs

        AppleIntelSNBGraphicsFB hard codes the supported Board IDs for Sandy Bridge iGPUs
        Because of this, the kext errors out on unsupported systems
        This function simply patches in a supported Board ID, using 'determine_best_board_id_for_sandy()'
        to supplement the ideal Board ID

        Parameters:
            source_files_path (str): Path to the source files

        """

        source_files_path = str(source_files_path)

        if self.constants.computer.reported_board_id in self.constants.sandy_board_id_stock:
            return

        logging.info(f"Found unsupported Board ID {self.constants.computer.reported_board_id}, performing AppleIntelSNBGraphicsFB bin patching")

        board_to_patch = generate_smbios.determine_best_board_id_for_sandy(self.constants.computer.reported_board_id, self.constants.computer.gpus)
        logging.info(f"Replacing {board_to_patch} with {self.constants.computer.reported_board_id}")

        board_to_patch_hex = bytes.fromhex(board_to_patch.encode('utf-8').hex())
        reported_board_hex = bytes.fromhex(self.constants.computer.reported_board_id.encode('utf-8').hex())

        if len(board_to_patch_hex) > len(reported_board_hex):
            # Pad the reported Board ID with zeros to match the length of the board to patch
            reported_board_hex = reported_board_hex + bytes(len(board_to_patch_hex) - len(reported_board_hex))
        elif len(board_to_patch_hex) < len(reported_board_hex):
            logging.info(f"Error: Board ID {self.constants.computer.reported_board_id} is longer than {board_to_patch}")
            raise Exception("Host's Board ID is longer than the kext's Board ID, cannot patch!!!")

        path = source_files_path + "/10.13.6/System/Library/Extensions/AppleIntelSNBGraphicsFB.kext/Contents/MacOS/AppleIntelSNBGraphicsFB"
        if not Path(path).exists():
            logging.info(f"Error: Could not find {path}")
            raise Exception("Failed to find AppleIntelSNBGraphicsFB.kext, cannot patch!!!")

        with open(path, 'rb') as f:
            data = f.read()
        data = data.replace(board_to_patch_hex, reported_board_hex)
        with open(path, 'wb') as f:
            f.write(data)


    def generate_patchset_plist(
        self,
        patchset: dict,
        file_name: str,
        kdk_used: Path,
        metallib_used: Path,
        output_dir: Optional[Union[str, Path]] = None,
        detected_os: Optional[int] = None,
        detected_os_minor: Optional[int] = None,
        detected_os_build: Optional[str] = None,
    ):
        """
        Generate patchset file for user reference

        Parameters:
            patchset (dict): Dictionary of patchset, sys_patch/patchsets
            file_name (str): Name of the file to write to
            kdk_used (Path): Path to the KDK used, if any

        Returns:
            bool: True if successful, False if not

        """

        if output_dir is not None:
            source_dir = Path(output_dir)
            source_dir.mkdir(parents=True, exist_ok=True)
        else:
            payload_dir = Path(self.constants.payload_path)
            if payload_dir.is_dir() and os.access(payload_dir, os.W_OK):
                source_dir = payload_dir
            else:
                source_dir = Path(tempfile.mkdtemp(prefix="ocpbp_plist_"))

        source_path_file = source_dir / file_name

        target_os = detected_os if detected_os is not None else self.constants.detected_os
        target_minor = detected_os_minor if detected_os_minor is not None else self.constants.detected_os_minor
        target_build = detected_os_build if detected_os_build is not None else self.constants.detected_os_build

        kdk_string = "Not applicable"
        if kdk_used:
            kdk_string = kdk_used

        metallib_used_string = "Not applicable"
        if metallib_used:
            metallib_used_string = metallib_used

        data = {
            "OpenCore Legacy Patcher": f"v{self.constants.patcher_version}",
            "PatcherSupportPkg": f"v{self.constants.patcher_support_pkg_version}",
            "Time Patched": f"{datetime.now().strftime('%B %d, %Y @ %H:%M:%S')}",
            "Commit URL": f"{self.constants.commit_info[2]}",
            "Kernel Debug Kit Used": f"{kdk_string}",
            "Metal Library Used": f"{metallib_used_string}",
            "OS Version": f"{target_os}.{target_minor} ({target_build})",
            "Custom Signature": bool(Path(self.constants.payload_local_binaries_root_path / ".signed").exists()),
        }

        data.update(patchset)

        if source_path_file.exists():
            os.remove(source_path_file)

        with source_path_file.open("wb") as f:
            plistlib.dump(data, f, sort_keys=False)

        self.last_generated_plist_path = source_path_file

        if source_path_file.exists():
            return True

        return False


    def disable_window_server_caching(
        self,
        target_data_mount: Optional[Union[str, Path]] = None,
        detected_os: Optional[int] = None,
    ):
        """
        Disable WindowServer's asset caching

        On legacy GCN GPUs, the WindowServer cache generated creates
        corrupted Opaque shaders.

        To work-around this, we disable WindowServer caching
        And force macOS into properly generating the Opaque shaders
        """

        target_os = detected_os if detected_os is not None else self.constants.detected_os
        if target_os < os_data.os_data.ventura:
            return

        if target_data_mount and str(target_data_mount) not in ("", "/"):
            logging.info("Clearing offline WindowServer caches (runtime chflags uchg deferred to first boot)")
            var_folders = Path(target_data_mount) / "private/var/folders"
            if var_folders.is_dir():
                for cache_dir in var_folders.glob("*/*/*/WindowServer/com.apple.WindowServer"):
                    subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(cache_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return

        logging.info("Disabling WindowServer Caching")
        # Invoke via 'bash -c' to resolve pathing
        subprocess_wrapper.run_as_root(["/bin/bash", "-c", "/bin/rm -rf /private/var/folders/*/*/*/WindowServer/com.apple.WindowServer"])
        # Disable writing to WindowServer folder
        subprocess_wrapper.run_as_root(["/bin/bash", "-c", "/usr/bin/chflags uchg /private/var/folders/*/*/*/WindowServer"])
        # Reference:
        #   To reverse write lock:
        #   'chflags nouchg /private/var/folders/*/*/*/WindowServer'


    def install_rsr_repair_binary(
        self,
        mount_location: Optional[Union[str, Path]] = None,
        detected_os: Optional[int] = None,
    ):
        """
        Installs RSRRepair

        RSRRepair is a utility that will sync the SysKC and BootKC in the event of a panic

        With macOS 13.2, Apple implemented the Rapid Security Response System
        However Apple added a half baked snapshot reversion system if seal was broken,
        which forgets to handle Preboot BootKC syncing.

        Thus this application will try to re-sync the BootKC with SysKC in the event of a panic
            Reference: https://github.com/dortania/OpenCore-Legacy-Patcher/issues/1019

        This is a (hopefully) temporary work-around, however likely to stay.
        RSRRepair has the added bonus of fixing desynced KCs from 'bless', so useful in Big Sur+
            Source: https://github.com/flagersgit/RSRRepair

        """

        target_os = detected_os if detected_os is not None else self.constants.detected_os
        if target_os < os_data.os_data.big_sur:
            return

        logging.info("Installing Kernel Collection syncing utility")
        if mount_location and str(mount_location) not in ("", "/"):
            mount_base = Path(mount_location)
            src = Path(self.constants.rsrrepair_userspace_path)
            if not src.exists():
                logging.info(f"- Failed to find RSRRepair binary at {src}")
                return

            target_etc = (mount_base / "private/etc") if (mount_base / "private/etc").is_dir() else (mount_base / "etc")
            if not target_etc.exists():
                subprocess_wrapper.run_as_root(["/bin/mkdir", "-p", str(target_etc)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            dest = target_etc / "rc.server"
            result = subprocess_wrapper.run_as_root(["/usr/bin/ditto", str(src), str(dest)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                logging.info("- Failed to install RSRRepair to target root")
                subprocess_wrapper.log(result)
                return
            subprocess_wrapper.run_as_root(["/bin/chmod", "755", str(dest)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess_wrapper.run_as_root(["/usr/sbin/chown", "root:wheel", str(dest)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return

        result = subprocess_wrapper.run_as_root([self.constants.rsrrepair_userspace_path, "--install"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            logging.info("- Failed to install RSRRepair")
            subprocess_wrapper.log(result)


    def patch_gpu_compiler_libraries(
        self,
        mount_point: Union[str, Path],
        detected_os: Optional[int] = None,
        detected_os_minor: Optional[int] = None,
    ):
        """
        Fix GPUCompiler.framework's libraries to resolve linking issues

        On 13.3 with 3802 GPUs, OCLP will downgrade GPUCompiler to resolve
        graphics support. However the binary hardcodes the library names,
        and thus we need to adjust the libraries to match (31001.669)

        Important portions of the library will be downgraded to 31001.669,
        and the remaining bins will be copied over (via CoW to reduce waste)

        Primary folders to merge:
        - 31001.XXX: (current OS version)
            - include:
                - module.modulemap
                - opencl-c.h
            - lib (entire directory)

        Note: With macOS Sonoma, 32023 compiler is used instead and so this patch is not needed
              until macOS 14.2 Beta 2 with version '32023.26'.

        Parameters:
            mount_point: The mount point of the target volume
        """
        target_os = detected_os if detected_os is not None else self.constants.detected_os
        target_minor = detected_os_minor if detected_os_minor is not None else self.constants.detected_os_minor

        if target_os < os_data.os_data.ventura:
            return

        if target_os == os_data.os_data.ventura:
            if target_minor < 4: # 13.3
                return
            BASE_VERSION = "31001"
            GPU_VERSION = f"{BASE_VERSION}.669"
        elif target_os == os_data.os_data.sonoma:
            if target_minor < 2: # 14.2 Beta 2
                return
            BASE_VERSION = "32023"
            GPU_VERSION = f"{BASE_VERSION}.26"
        else:
            # Fall back for newer versions
            BASE_VERSION = "32023"
            GPU_VERSION = f"{BASE_VERSION}.26"

        LIBRARY_DIR = f"{mount_point}/System/Library/PrivateFrameworks/GPUCompiler.framework/Versions/{BASE_VERSION}/Libraries/lib/clang"
        DEST_DIR = f"{LIBRARY_DIR}/{GPU_VERSION}"

        if not Path(DEST_DIR).exists():
            raise Exception(f"Failed to find GPUCompiler libraries at {DEST_DIR}")

        for file in Path(LIBRARY_DIR).iterdir():
            if file.is_file():
                continue
            if file.name == GPU_VERSION:
                continue

            # Partial match as each OS can increment the version
            if not file.name.startswith(f"{BASE_VERSION}."):
                continue

            logging.info(f"Merging GPUCompiler.framework libraries to match binary")

            src_dir = f"{LIBRARY_DIR}/{file.name}"
            if not Path(f"{DEST_DIR}/lib").exists():
                subprocess_wrapper.run_as_root_and_verify(generate_copy_arguments(f"{src_dir}/lib", f"{DEST_DIR}/"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            break
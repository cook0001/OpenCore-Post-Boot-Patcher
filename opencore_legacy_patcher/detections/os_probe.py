"""
os_probe.py: OS Host information
"""

import plistlib
import platform
import re
import subprocess

from pathlib import Path
from typing import Optional, Union

from ..datasets import os_data


class OSProbe:
    """
    Library for querying OS information specific to macOS
    """

    def __init__(
        self,
        target_root: Optional[Union[str, Path]] = None,
        preboot_root: Optional[Union[str, Path]] = None,
    ) -> None:
        self.uname_data = platform.uname()
        self.target_root: Optional[Path] = self._normalize_target_root(target_root)
        self.preboot_root: Optional[Path] = Path(preboot_root) if preboot_root else None


    @staticmethod
    def _normalize_target_root(target_root: Optional[Union[str, Path]]) -> Optional[Path]:
        if target_root is None:
            return None
        path = Path(target_root)
        if str(path) in ("", "/"):
            return None
        return path


    def _load_system_version_plist(self, plist_path: Path) -> dict:
        try:
            with plist_path.open("rb") as f:
                return plistlib.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to parse SystemVersion.plist at {plist_path}: {e}")


    def _find_target_rsr_system_version_plist(self) -> Optional[Path]:
        candidates: list[Path] = []
        if self.preboot_root is not None:
            candidates.append(self.preboot_root / "Cryptexes/OS/System/Library/CoreServices/SystemVersion.plist")
            if self.preboot_root.exists():
                try:
                    for sub in sorted(self.preboot_root.iterdir()):
                        if sub.is_dir():
                            candidates.append(sub / "cryptex1/current/SystemVersion.plist")
                            candidates.append(sub / "Cryptexes/OS/System/Library/CoreServices/SystemVersion.plist")
                            candidates.append(sub / "System/Library/CoreServices/SystemVersion.plist")
                except OSError:
                    pass
        if self.target_root is not None:
            candidates.append(self.target_root / "System/Volumes/Preboot/Cryptexes/OS/System/Library/CoreServices/SystemVersion.plist")

        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None


    def _detect_target_xnu_tuple(self) -> tuple[int, int]:
        assert self.target_root is not None

        system_kext_plist = self.target_root / "System/Library/Extensions/System.kext/Contents/Info.plist"
        if system_kext_plist.is_file():
            try:
                with system_kext_plist.open("rb") as f:
                    kext_info = plistlib.load(f)
                for key in ("CFBundleVersion", "CFBundleShortVersionString"):
                    raw_ver = str(kext_info.get(key, "")).strip()
                    match = re.match(r"^(\d+)\.(\d+)", raw_ver)
                    if match:
                        return int(match.group(1)), int(match.group(2))
            except Exception:
                pass

        os_version = self.detect_os_version()
        try:
            kernel_major = os_data.os_conversion.os_to_kernel(os_version)
            kernel_minor = os_data.os_conversion.os_to_kernel_minor(os_version)
            return kernel_major, kernel_minor
        except Exception as e:
            raise RuntimeError(f"Failed to derive XNU version from target OS {os_version}: {e}")


    def detect_kernel_major(self) -> int:
        """
        Detect the booted or target major kernel version

        Returns:
            int: Major kernel version (ex. 21, from 21.1.0)
        """
        if self.target_root is not None:
            return self._detect_target_xnu_tuple()[0]

        return int(self.uname_data.release.partition(".")[0])


    def detect_kernel_minor(self) -> int:
        """
        Detect the booted or target minor kernel version

        Returns:
            int: Minor kernel version (ex. 1, from 21.1.0)
        """
        if self.target_root is not None:
            return self._detect_target_xnu_tuple()[1]

        return int(self.uname_data.release.partition(".")[2].partition(".")[0])


    def detect_os_version(self, rsr: bool = False) -> str:
        """
        Detect the booted or target OS version

        Returns:
            str: OS version (ex. 12.0)
        """
        if self.target_root is not None:
            plist_path = self.target_root / "System/Library/CoreServices/SystemVersion.plist"
            if rsr is True:
                rsr_plist = self._find_target_rsr_system_version_plist()
                if rsr_plist is not None:
                    plist_path = rsr_plist
            data = self._load_system_version_plist(plist_path)
            version_value = data.get("ProductVersion")
            if not version_value:
                raise RuntimeError(f"Missing ProductVersion in {plist_path}")
            return str(version_value).strip()

        result = subprocess.run(["/usr/bin/sw_vers", "-productVersion"], stdout=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError("Failed to detect OS version")

        return result.stdout.decode().strip()


    def detect_os_build(self, rsr: bool = False) -> str:
        """
        Detect the booted or target OS build

        Implementation note:
            With macOS 13.2, Apple implemented the Rapid Security Response system which
            will change the reported build to the RSR version and not the original host

            To get the proper versions:
            - Host: /System/Library/CoreServices/SystemVersion.plist
            - RSR:  /System/Volumes/Preboot/Cryptexes/OS/System/Library/CoreServices/SystemVersion.plist


        Parameters:
            rsr (bool): Whether to use the RSR version of the build

        Returns:
            str: OS build (ex. 21A5522h)
        """
        if self.target_root is not None:
            plist_path = self.target_root / "System/Library/CoreServices/SystemVersion.plist"
            if rsr is True:
                rsr_plist = self._find_target_rsr_system_version_plist()
                if rsr_plist is not None:
                    plist_path = rsr_plist
            data = self._load_system_version_plist(plist_path)
            build_value = data.get("ProductBuildVersion")
            if not build_value:
                raise RuntimeError(f"Missing ProductBuildVersion in {plist_path}")
            return str(build_value).strip()

        file_path = "/System/Library/CoreServices/SystemVersion.plist"
        if rsr is True:
            file_path = f"/System/Volumes/Preboot/Cryptexes/OS{file_path}"

        try:
            return plistlib.load(open(file_path, "rb"))["ProductBuildVersion"]
        except Exception as e:
            raise RuntimeError(f"Failed to detect OS build: {e}")

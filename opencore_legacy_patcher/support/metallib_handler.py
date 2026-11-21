"""
metallib_handler.py: Library for handling Metal libraries
"""

import logging
import os
import plistlib
import requests
import subprocess
import packaging.version

from typing  import Optional, Union, cast
from pathlib import Path

from .  import network_handler, subprocess_wrapper
from .. import constants

from ..datasets import os_data


METALLIB_INSTALL_PATH: str  = "/Library/Application Support/Dortania/MetallibSupportPkg"
METALLIB_INFO_PLIST:   str  = "MetallibInfo.plist"
METALLIB_API_LINK:     str  = "https://dortania.github.io/MetallibSupportPkg/manifest.json"

METALLIB_ASSET_LIST:   list = None


class MetalLibraryObject:

    def __init__(self, global_constants: constants.Constants,
                 host_build: str, host_version: str,
                 ignore_installed: bool = False, passive: bool = False,
                 metallib_install_path: Optional[Union[str, Path]] = None,
                 offline_assets_dir: Optional[Union[str, Path]] = None,
                 target_volume: Optional[Union[str, Path]] = None,
        ) -> None:

        self.constants: constants.Constants = global_constants

        self.host_build:   str = host_build    # ex. 20A5384c
        self.host_version: str = host_version  # ex. 11.0.1

        self.passive: bool = passive  # Don't perform actions requiring elevated privileges

        self.ignore_installed:      bool = ignore_installed   # If True, will ignore any installed MetallibSupportPkg PKGs and download the latest
        self.metallib_install_path: Path = Path(metallib_install_path) if metallib_install_path else Path(METALLIB_INSTALL_PATH)
        self.target_volume:         Path = Path(target_volume) if target_volume else Path("/")

        constants_offline = getattr(global_constants, "offline_assets_dir", None)
        resolved_offline = offline_assets_dir if offline_assets_dir is not None else constants_offline
        self.offline_assets_dir: Optional[Path] = Path(resolved_offline) if resolved_offline else None

        self.metallib_already_installed: bool = False
        self.metallib_available_offline: bool = False
        self.metallib_offline_asset_path: str = ""
        self.metallib_offline_asset_type: str = ""

        self.metallib_installed_path: str = ""

        self.metallib_url:         str = ""
        self.metallib_url_build:   str = ""
        self.metallib_url_version: str = ""

        self.metallib_url_is_exactly_match: bool = False

        self.metallib_closest_match_url:         str = ""
        self.metallib_closest_match_url_build:   str = ""
        self.metallib_closest_match_url_version: str = ""

        self.success: bool = False

        self.error_msg: str = ""

        self._get_latest_metallib()


    @property
    def metallib_available(self) -> bool:
        return bool(self.metallib_already_installed or self.metallib_available_offline)


    def _offline_search_dirs(self) -> list[Path]:
        if not self.offline_assets_dir:
            return []
        candidates = [
            self.offline_assets_dir,
            self.offline_assets_dir / "MetallibSupportPkg",
            self.offline_assets_dir / "assets",
        ]
        return [d for d in candidates if d.is_dir()]


    @staticmethod
    def _read_metallib_info_plist(directory: Path) -> Optional[dict]:
        info_path = directory / METALLIB_INFO_PLIST
        if not info_path.is_file():
            return None
        try:
            with info_path.open("rb") as f:
                return plistlib.load(f)
        except Exception:
            return None


    def _find_offline_metallib(self, match: Optional[str] = None, check_version: bool = False, allow_generic: bool = False) -> Optional[tuple[Path, str]]:
        if self.ignore_installed is True:
            return None
        search_dirs = self._offline_search_dirs()
        if not search_dirs:
            return None

        target_match = match or (self.host_version if check_version else self.host_build)

        for search_dir in search_dirs:
            for item in sorted(search_dir.iterdir()):
                if not item.is_dir():
                    continue
                if check_version:
                    if target_match not in item.name:
                        continue
                else:
                    if not item.name.endswith(f"-{target_match}"):
                        continue
                if any(item.rglob("*.metallib")):
                    return (item, "dir")

        for search_dir in search_dirs:
            info = self._read_metallib_info_plist(search_dir)
            for item in sorted(search_dir.iterdir()):
                if item.is_dir() or not item.name.endswith(".pkg"):
                    continue
                if "metallib" not in item.name.lower():
                    continue
                if check_version:
                    if target_match in item.name:
                        return (item, "pkg")
                    if info and target_match in str(info.get("version", "")):
                        return (item, "pkg")
                else:
                    if target_match in item.name:
                        return (item, "pkg")
                    if info and info.get("build") == target_match:
                        return (item, "pkg")

        if allow_generic:
            for search_dir in search_dirs:
                candidate = search_dir / "MetallibSupportPkg.pkg"
                if candidate.is_file():
                    return (candidate, "pkg")

        return None


    def _apply_offline_metallib_match(self, asset_path: Path, asset_type: str) -> None:
        if asset_type == "dir":
            logging.info(f"Found offline metallib directory: {asset_path}")
            self.metallib_installed_path = str(asset_path)
            self.metallib_already_installed = True
            self.success = True
            return

        logging.info(f"Found offline metallib pkg: {asset_path}")
        self.metallib_offline_asset_path = str(asset_path)
        self.metallib_offline_asset_type = asset_type
        self.metallib_available_offline = True
        self.success = True


    def _get_remote_metallibs(self) -> dict:
        """
        Get the MetallibSupportPkg list from the API
        """

        global METALLIB_ASSET_LIST

        logging.info("Pulling metallib list from MetallibSupportPkg API")
        if METALLIB_ASSET_LIST:
            return METALLIB_ASSET_LIST

        try:
            results = network_handler.NetworkUtilities().get(
                METALLIB_API_LINK,
                headers={
                    "User-Agent": f"OCLP/{self.constants.patcher_version}"
                },
                timeout=5
            )
        except (requests.exceptions.Timeout, requests.exceptions.TooManyRedirects, requests.exceptions.ConnectionError):
            logging.info("Could not contact MetallibSupportPkg API")
            return None

        if results.status_code != 200:
            logging.info("Could not fetch Metallib list")
            return None

        METALLIB_ASSET_LIST = results.json()

        return METALLIB_ASSET_LIST


    def _get_latest_metallib(self) -> None:
        """
        Get the latest MetallibSupportPkg PKG
        """

        parsed_version = cast(packaging.version.Version, packaging.version.parse(self.host_version))

        if os_data.os_conversion.os_to_kernel(str(parsed_version.major)) < os_data.os_data.sequoia:
            self.error_msg = "MetallibSupportPkg is not required for macOS Sonoma or older"
            logging.warning(f"{self.error_msg}")
            return

        self.metallib_installed_path = self._local_metallib_installed()
        if self.metallib_installed_path:
            logging.info(f"metallib already installed ({Path(self.metallib_installed_path).name}), skipping")
            self.metallib_already_installed = True
            self.success = True
            return

        offline_exact = self._find_offline_metallib(match=self.host_build, check_version=False, allow_generic=False)
        if offline_exact:
            self._apply_offline_metallib_match(*offline_exact)
            return

        if self.offline_assets_dir is not None:
            loose_version = f"{parsed_version.major}.{parsed_version.minor}"
            offline_loose = self._find_offline_metallib(match=loose_version, check_version=True, allow_generic=True)
            if offline_loose:
                self._apply_offline_metallib_match(*offline_loose)
                return

        remote_metallib_version = self._get_remote_metallibs()

        if remote_metallib_version is None:
            logging.warning("Failed to fetch metallib list, falling back to local metallib matching")

            # First check if a metallib matching the current macOS version is installed
            # ex. 13.0.1 vs 13.0
            loose_version = f"{parsed_version.major}.{parsed_version.minor}"
            logging.info(f"Checking for metallibs loosely matching {loose_version}")
            self.metallib_installed_path = self._local_metallib_installed(match=loose_version, check_version=True)
            if self.metallib_installed_path:
                logging.info(f"Found matching metallib: {Path(self.metallib_installed_path).name}")
                self.metallib_already_installed = True
                self.success = True
                return

            older_version = f"{parsed_version.major}.{parsed_version.minor - 1 if parsed_version.minor > 0 else 0}"
            logging.info(f"Checking for metallibs matching {older_version}")
            self.metallib_installed_path = self._local_metallib_installed(match=older_version, check_version=True)
            if self.metallib_installed_path:
                logging.info(f"Found matching metallib: {Path(self.metallib_installed_path).name}")
                self.metallib_already_installed = True
                self.success = True
                return

            offline_fallback = self._find_offline_metallib(match=older_version, check_version=True, allow_generic=True)
            if offline_fallback:
                self._apply_offline_metallib_match(*offline_fallback)
                return

            logging.warning(f"Couldn't find metallib matching {self.host_version} or {older_version}, please install one manually")

            self.error_msg = f"Could not contact MetallibSupportPkg API, and no metallib matching {self.host_version} ({self.host_build}) or {older_version} was installed.\nPlease ensure you have a network connection or manually install a metallib."

            return


        # First check exact match
        for metallib in remote_metallib_version:
            if (metallib["build"] != self.host_build):
                continue
            self.metallib_url = metallib["url"]
            self.metallib_url_build = metallib["build"]
            self.metallib_url_version = metallib["version"]
            self.metallib_url_is_exactly_match = True
            break

        # If no exact match, check for closest match
        if self.metallib_url == "":
            for metallib in remote_metallib_version:
                metallib_version = cast(packaging.version.Version, packaging.version.parse(metallib["version"]))
                if metallib_version > parsed_version:
                    continue
                if metallib_version.major != parsed_version.major:
                    continue
                if metallib_version.minor not in range(parsed_version.minor - 1, parsed_version.minor + 1):
                    continue

                # The metallib list is already sorted by version then date, so the first match is the closest
                self.metallib_closest_match_url = metallib["url"]
                self.metallib_closest_match_url_build = metallib["build"]
                self.metallib_closest_match_url_version = metallib["version"]
                self.metallib_url_is_exactly_match = False
                break

        if self.metallib_url == "":
            if self.metallib_closest_match_url == "":
                logging.warning(f"No metallibs found for {self.host_build} ({self.host_version})")
                self.error_msg = f"No metallibs found for {self.host_build} ({self.host_version})"
                return
            logging.info(f"No direct match found for {self.host_build}, falling back to closest match")
            logging.info(f"Closest Match: {self.metallib_closest_match_url_build} ({self.metallib_closest_match_url_version})")

            self.metallib_url = self.metallib_closest_match_url
            self.metallib_url_build = self.metallib_closest_match_url_build
            self.metallib_url_version = self.metallib_closest_match_url_version
        else:
            logging.info(f"Direct match found for {self.host_build} ({self.host_version})")


        # Check if this metallib is already installed
        self.metallib_installed_path = self._local_metallib_installed(match=self.metallib_url_build)
        if self.metallib_installed_path:
            logging.info(f"metallib already installed ({Path(self.metallib_installed_path).name}), skipping")
            self.metallib_already_installed = True
            self.success = True
            return

        logging.info("Following metallib is recommended:")
        logging.info(f"- metallib Build: {self.metallib_url_build}")
        logging.info(f"- metallib Version: {self.metallib_url_version}")
        logging.info(f"- metallib URL: {self.metallib_url}")

        self.success = True


    def _local_metallib_installed(self, match: str = None, check_version: bool = False) -> str:
        """
        Check if a metallib is already installed
        """

        if self.ignore_installed:
            return None

        if match is None:
            match = self.host_version if check_version else self.host_build

        if not self.metallib_install_path.exists():
            return None

        for metallib_folder in self.metallib_install_path.iterdir():
            if not metallib_folder.is_dir():
                continue
            if check_version:
                if match not in metallib_folder.name:
                    continue
            else:
                if not metallib_folder.name.endswith(f"-{match}"):
                    continue

            return metallib_folder

        return None


    def retrieve_download(self, override_path: str = "") -> network_handler.DownloadObject:
        """
        Retrieve MetallibSupportPkg PKG download object
        """

        self.success = False
        self.error_msg = ""

        if self.metallib_already_installed:
            logging.info("No download required, metallib already installed")
            self.success = True
            return None

        if self.metallib_url == "":
            self.error_msg = "Could not retrieve metallib catalog, no metallib to download"
            logging.error(self.error_msg)
            return None

        logging.info(f"Returning DownloadObject for metallib: {Path(self.metallib_url).name}")
        self.success = True

        metallib_download_path = self.constants.metallib_download_path if override_path == "" else Path(override_path)
        return network_handler.DownloadObject(self.metallib_url, metallib_download_path)


    def _extract_metallib_pkg_offline(self, pkg_path: Path, dest_dir: Path) -> bool:
        """
        Extract MetallibSupportPkg.pkg directly into `<target_data>/Library/Application Support/Dortania/MetallibSupportPkg`
        using a staging folder on `dest_dir`.
        """
        subprocess_wrapper.run_as_root(["/bin/mkdir", "-p", str(dest_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stage_dir = dest_dir / f".ocpbp_metallib_stage_{os.getpid()}"
        if stage_dir.exists():
            subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(stage_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        expanded_dir = stage_dir / "expanded"
        try:
            result = subprocess_wrapper.run_as_root(
                ["/usr/sbin/pkgutil", "--expand-full", str(pkg_path), str(expanded_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                logging.warning("pkgutil --expand-full failed for offline MetallibSupportPkg")
                subprocess_wrapper.log(result)
                return False

            installed_dirs: list[Path] = []
            for payload_root in expanded_dir.rglob("MetallibSupportPkg"):
                if payload_root.is_dir():
                    for child in payload_root.iterdir():
                        if child.is_dir():
                            installed_dirs.append(child)

            if not installed_dirs:
                for candidate in expanded_dir.rglob("*"):
                    if candidate.is_dir() and any(candidate.glob("System")) and candidate.name != "Payload":
                        installed_dirs.append(candidate)

            if not installed_dirs:
                logging.warning(f"No MetallibSupportPkg directories found inside expanded package: {pkg_path}")
                return False

            for src_dir in installed_dirs:
                dst_subdir = dest_dir / src_dir.name
                if dst_subdir.exists():
                    subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(dst_subdir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                move_res = subprocess_wrapper.run_as_root(
                    ["/bin/mv", str(src_dir), str(dst_subdir)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                if move_res.returncode != 0:
                    subprocess_wrapper.log(move_res)
                    return False

            return True
        finally:
            if stage_dir.exists():
                subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(stage_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


    def install_metallib(
        self,
        metallib: Optional[Union[str, Path]] = None,
        target_volume: Optional[Union[str, Path]] = None,
        metallib_install_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Install MetallibSupportPkg PKG
        """

        if not self.success and not metallib:
            logging.error("Cannot install metallib, no metallib was successfully retrieved")
            return False

        if self.metallib_already_installed:
            logging.info("No installation required, metallib already installed")
            return True

        pkg_path = Path(metallib or self.metallib_offline_asset_path or self.constants.metallib_download_path)
        resolved_target = str(target_volume or self.target_volume or "/")
        resolved_install_dir = Path(metallib_install_path) if metallib_install_path else (
            Path(resolved_target) / "Library/Application Support/Dortania/MetallibSupportPkg"
            if resolved_target != "/"
            else self.metallib_install_path
        )

        if resolved_install_dir != Path(METALLIB_INSTALL_PATH) or resolved_target != "/":
            if self._extract_metallib_pkg_offline(pkg_path, resolved_install_dir):
                return True
            if resolved_target == "/":
                return False

        result = subprocess_wrapper.run_as_root([
            "/usr/sbin/installer", "-pkg", pkg_path, "-target", resolved_target
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            subprocess_wrapper.log(result)
            return False

        return True
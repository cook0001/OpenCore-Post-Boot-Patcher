"""
dmg_mount.py: PatcherSupportPkg DMG Mounting. Handles Universal-Binaries and DortaniaInternalResources DMGs.
"""

import atexit
import logging
import os
import subprocess
import sys
import getpass
import tempfile

from pathlib import Path
from typing import Optional, Union

from ... import constants

from ...support import subprocess_wrapper


class PatcherSupportPkgMount:

    def __init__(
        self,
        global_constants: constants.Constants,
        offline_assets_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.constants: constants.Constants = global_constants
        self.icon_path = str(self.constants.app_icon_path).replace("/", ":")[1:]
        constants_offline = getattr(global_constants, "offline_assets_dir", None)
        resolved_offline = offline_assets_dir if offline_assets_dir is not None else constants_offline
        self.offline_assets_dir: Optional[Path] = Path(resolved_offline) if resolved_offline else None
        self._mounted_dmg_point: Optional[Path] = None
        self._temp_mount_base: Optional[Path] = None
        self._set_override: bool = False


    def _resolve_universal_binaries_dmg(self) -> Optional[Path]:
        if self.offline_assets_dir:
            for candidate in (
                self.offline_assets_dir / "Universal-Binaries.dmg",
                self.offline_assets_dir / "assets" / "Universal-Binaries.dmg",
            ):
                if candidate.is_file():
                    return candidate
        dmg_path = Path(self.constants.payload_local_binaries_root_path_dmg)
        if dmg_path.is_file():
            return dmg_path
        return None


    def _resolve_unpacked_universal_binaries_dir(self) -> Optional[Path]:
        if self.offline_assets_dir:
            for candidate in (
                self.offline_assets_dir / "Universal-Binaries",
                self.offline_assets_dir / "assets" / "Universal-Binaries",
            ):
                if candidate.is_dir():
                    return candidate
        if Path(self.constants.payload_local_binaries_root_path).is_dir():
            return Path(self.constants.payload_local_binaries_root_path)
        return None


    def _mount_universal_binaries_dmg(self) -> bool:
        """
        Mount PatcherSupportPkg's Universal-Binaries.dmg
        """
        dmg_path = self._resolve_universal_binaries_dmg()
        if dmg_path is None:
            logging.info("- PatcherSupportPkg resources missing, Patcher likely corrupted!!!")
            return False

        payload_dir = Path(self.constants.payload_path)
        if payload_dir.is_dir() and os.access(payload_dir, os.W_OK):
            mount_point = payload_dir / "Universal-Binaries"
            shadow_path = payload_dir / "Universal-Binaries_overlay"
        else:
            self._temp_mount_base = Path(tempfile.mkdtemp(prefix="ocpbp_ub_"))
            mount_point = self._temp_mount_base / "Universal-Binaries"
            shadow_path = self._temp_mount_base / "Universal-Binaries_overlay"
            mount_point.mkdir(parents=True, exist_ok=True)

        output = subprocess.run(
            [
                "/usr/bin/hdiutil", "attach", "-noverify", str(dmg_path),
                "-mountpoint", mount_point,
                "-nobrowse",
                "-shadow", shadow_path,
                "-passphrase", "password"
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if output.returncode != 0:
            logging.info("- Failed to mount Universal-Binaries.dmg")
            subprocess_wrapper.log(output)
            return False

        self._mounted_dmg_point = mount_point
        self.constants._payload_local_binaries_root_path_override = mount_point
        self._set_override = True
        atexit.register(self.unmount)
        logging.info(f"- Mounted Universal-Binaries.dmg from {dmg_path}")
        return True


    def unmount(self) -> None:
        if self._set_override:
            self.constants._payload_local_binaries_root_path_override = None
            self._set_override = False
        if self._mounted_dmg_point and self._mounted_dmg_point.exists():
            subprocess.run(
                ["/usr/bin/hdiutil", "detach", str(self._mounted_dmg_point), "-force"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._mounted_dmg_point = None
        if self._temp_mount_base and self._temp_mount_base.exists():
            for name in ("Universal-Binaries_overlay",):
                overlay = self._temp_mount_base / name
                if overlay.exists():
                    try:
                        overlay.unlink()
                    except OSError:
                        pass
            try:
                ub_dir = self._temp_mount_base / "Universal-Binaries"
                if ub_dir.exists():
                    ub_dir.rmdir()
                self._temp_mount_base.rmdir()
            except OSError:
                pass
            self._temp_mount_base = None


    def _mount_dortania_internal_resources_dmg(self) -> bool:
        """
        Mount PatcherSupportPkg's DortaniaInternalResources.dmg (if available)
        """
        if not Path(self.constants.overlay_psp_path_dmg).exists():
            return True
        if not Path("~/.dortania_developer").expanduser().exists():
            return True
        if self.constants.cli_mode is True:
            return True

        logging.info("- Found DortaniaInternal resources, mounting...")

        for i in range(3):
            key = self._request_decryption_key(i)
            output = subprocess.run(
                [
                    "/usr/bin/hdiutil", "attach", "-noverify", f"{self.constants.overlay_psp_path_dmg}",
                    "-mountpoint", Path(self.constants.payload_path / Path("DortaniaInternal")),
                    "-nobrowse",
                    "-passphrase", key
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            if output.returncode != 0:
                logging.info("- Failed to mount DortaniaInternal resources")
                subprocess_wrapper.log(output)

                if "Authentication error" not in output.stdout.decode():
                    self._display_authentication_error()

                if i == 2:
                    self._display_too_many_attempts()
                    return False
                continue
            break

        logging.info("- Mounted DortaniaInternal resources")
        return self._merge_dortania_internal_resources()


    def _merge_dortania_internal_resources(self) -> bool:
        """
        Merge DortaniaInternal resources with Universal-Binaries
        """
        result = subprocess.run(
            [
                "/usr/bin/ditto", f"{self.constants.payload_path / Path('DortaniaInternal')}", f"{self.constants.payload_local_binaries_root_path}"
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if result.returncode != 0:
            logging.info("- Failed to merge DortaniaInternal resources")
            subprocess_wrapper.log(result)
            return False

        return True


    def _request_decryption_key(self, attempt: int) -> str:
        """
        Fetch the decryption key for DortaniaInternalResources.dmg
        """
        # Only return on first attempt
        if attempt == 0:
            if Path("~/.dortania_developer_key").expanduser().exists():
                return Path("~/.dortania_developer_key").expanduser().read_text().strip()

        password = ""
        msg = "Welcome to the DortaniaInternal Program, please provide the decryption key to access internal resources. Press cancel to skip."
        if attempt > 0:
            msg = f"Decryption failed, please try again. {2 - attempt} attempts remaining. "

        try:
            import applescript
            password = applescript.AppleScript(
                f"""
                set theResult to display dialog "{msg}" default answer "" with hidden answer with title "OpenCore Legacy Patcher" with icon file "{self.icon_path}"

                return the text returned of theResult
                """
            ).run()
        except ImportError:
            if sys.stdin is not None and sys.stdin.isatty():
                try:
                    password = getpass.getpass(f"{msg}\nDecryption key: ")
                except Exception:
                    pass
        except Exception as e:
            pass

        return password


    def _display_authentication_error(self) -> None:
        """
        Display authentication error dialog
        """
        try:
            import applescript
            applescript.AppleScript(
                f"""
                display dialog "Failed to mount DortaniaInternal resources, please file an internal radar." with title "OpenCore Legacy Patcher" with icon file "{self.icon_path}"
                """
            ).run()
        except Exception as e:
            logging.error("Failed to mount DortaniaInternal resources.")


    def _display_too_many_attempts(self) -> None:
        """
        Display too many attempts dialog
        """
        try:
            import applescript
            applescript.AppleScript(
                f"""
                display dialog "Failed to mount DortaniaInternal resources, too many incorrect passwords. If this continues with the correct decryption key, please file an internal radar." with title "OpenCore Legacy Patcher" with icon file "{self.icon_path}"
                """
            ).run()
        except Exception as e:
            logging.error("Failed to mount DortaniaInternal resources, too many incorrect passwords.")


    def mount(self) -> bool:
        """
        Mount PatcherSupportPkg resources

        Returns:
            bool: True if all resources are mounted, False otherwise
        """
        # If already mounted or unpacked offline, use directly
        existing_dir = self._resolve_unpacked_universal_binaries_dir()
        if existing_dir is not None:
            self.constants._payload_local_binaries_root_path_override = existing_dir
            self._set_override = True
            logging.info("- Local PatcherSupportPkg resources available, continuing...")
            return True

        if self._mount_universal_binaries_dmg() is False:
            return False

        if self._mount_dortania_internal_resources_dmg() is False:
            return False

        return True
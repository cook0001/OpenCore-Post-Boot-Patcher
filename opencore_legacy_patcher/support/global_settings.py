"""
global_settings.py: Library for querying and writing global enviroment settings

Alternative to Apple's 'defaults' tool
Store data in '/Users/Shared'
This is to ensure compatibility when running without a user
ie. during automated patching
"""

import logging
import plistlib

from pathlib import Path
from typing import Optional


class GlobalEnviromentSettings:
    """
    Library for querying and writing global enviroment settings
    """

    def __init__(self, global_settings_folder: Optional[str] = None) -> None:
        self.file_name:              str = ".com.dortania.opencore-legacy-patcher.plist"
        self.global_settings_folder: str = global_settings_folder or "/Users/Shared"
        self.global_settings_plist:  str = f"{self.global_settings_folder}/{self.file_name}"

        self._generate_settings_file()
        self._convert_defaults_to_global_settings()


    def read_property(self, property_name: str) -> str:
        """
        Reads a property from the global settings file
        """

        plist_path = Path(self.global_settings_plist)
        if plist_path.exists():
            try:
                with plist_path.open("rb") as f:
                    plist = plistlib.load(f)
            except Exception as e:
                logging.error("Error: Unable to read global settings file")
                logging.error(e)
                return None
            if property_name in plist:
                return plist[property_name]
        return None


    def delete_property(self, property_name: str) -> None:
        """
        Deletes a property from the global settings file
        """
        plist_path = Path(self.global_settings_plist)
        if plist_path.exists():
            try:
                with plist_path.open("rb") as f:
                    plist = plistlib.load(f)
            except Exception as e:
                logging.error("Error: Unable to read global settings file")
                logging.error(e)
                return
            if property_name in plist:
                del plist[property_name]
                try:
                    with plist_path.open("wb") as f:
                        plistlib.dump(plist, f)
                except (PermissionError, FileNotFoundError, OSError):
                    logging.info("Failed to write to global settings")


    def write_property(self, property_name: str, property_value) -> None:
        """
        Writes a property to the global settings file
        """

        plist_path = Path(self.global_settings_plist)
        if plist_path.exists():
            try:
                with plist_path.open("rb") as f:
                    plist = plistlib.load(f)
            except Exception as e:
                logging.error("Error: Unable to read global settings file")
                logging.error(e)
                return
            plist[property_name] = property_value
            try:
                with plist_path.open("wb") as f:
                    plistlib.dump(plist, f)
            except (PermissionError, FileNotFoundError, OSError):
                logging.info("Failed to write to global settings file")


    def _generate_settings_file(self) -> None:
        plist_path = Path(self.global_settings_plist)
        if plist_path.exists():
            return
        if not plist_path.parent.exists():
            logging.info(f"Global settings folder unavailable, skipping creation: {plist_path.parent}")
            return
        try:
            with plist_path.open("wb") as f:
                plistlib.dump({"Developed by Dortania": True,}, f)
        except (PermissionError, FileNotFoundError, OSError):
            logging.info("Permission error: Unable to write to global settings file")


    def _convert_defaults_to_global_settings(self) -> None:
        """
        Converts legacy defaults to global settings
        """

        defaults_path = "~/Library/Preferences/com.dortania.opencore-legacy-patcher.plist"
        try:
            defaults_path = Path(defaults_path).expanduser()
        except RuntimeError:
            return

        plist_path = Path(self.global_settings_plist)
        if defaults_path.exists() and plist_path.exists():
            # merge defaults with global settings
            try:
                with defaults_path.open("rb") as f:
                    defaults_plist = plistlib.load(f)
                with plist_path.open("rb") as f:
                    global_settings_plist = plistlib.load(f)
            except Exception as e:
                logging.error("Error: Unable to read global settings file")
                logging.error(e)
                return
            global_settings_plist.update(defaults_plist)
            try:
                with plist_path.open("wb") as f:
                    plistlib.dump(global_settings_plist, f)
            except (PermissionError, FileNotFoundError, OSError):
                logging.info("Permission error: Unable to write to global settings file")
                return

            # delete defaults plist
            try:
                defaults_path.unlink()
            except Exception as e:
                logging.error("Error: Unable to delete defaults plist")
                logging.error(e)
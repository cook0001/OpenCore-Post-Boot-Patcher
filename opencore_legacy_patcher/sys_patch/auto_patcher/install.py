"""
install.py: Install the auto patcher launch services
"""

import hashlib
import logging
import os
import plistlib
import subprocess
import tempfile

from pathlib import Path
from typing import Optional, Union

from ... import constants

from ...volume import generate_copy_arguments

from ...support import (
    utilities,
    subprocess_wrapper
)


class InstallAutomaticPatchingServices:
    """
    Install the auto patcher launch services
    """

    def __init__(
        self,
        global_constants: constants.Constants,
        target_data_mount: Optional[Union[str, Path]] = None,
        preboot_mount: Optional[Union[str, Path]] = None,
        offline_assets_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.constants: constants.Constants = global_constants
        self.target_data_mount: Optional[Path] = (
            Path(target_data_mount) if (target_data_mount and str(target_data_mount) not in ("", "/")) else None
        )
        self.preboot_mount: Optional[Path] = Path(preboot_mount) if preboot_mount else None
        constants_offline = getattr(global_constants, "offline_assets_dir", None)
        resolved_offline = offline_assets_dir if offline_assets_dir is not None else constants_offline
        self.offline_assets_dir: Optional[Path] = Path(resolved_offline) if resolved_offline else None
        self.is_offline_target: bool = self.target_data_mount is not None


    def _data_root(self) -> Path:
        return self.target_data_mount if self.is_offline_target else Path("/")


    def _find_offline_pkg_or_app(self) -> tuple[Optional[Path], Optional[str]]:
        search_dirs: list[Path] = []
        if self.offline_assets_dir and self.offline_assets_dir.is_dir():
            search_dirs.append(self.offline_assets_dir)
            assets_sub = self.offline_assets_dir / "assets"
            if assets_sub.is_dir():
                search_dirs.append(assets_sub)

        for search_dir in search_dirs:
            app_dir = search_dir / "OpenCore-Patcher.app"
            if app_dir.is_dir() and (app_dir / "Contents").is_dir():
                return (app_dir, "app")
            for pkg_name in ("AutoPkg-Assets.pkg", "OpenCore-Patcher.pkg"):
                pkg_file = search_dir / pkg_name
                if pkg_file.is_file():
                    return (pkg_file, "pkg")
            zip_file = search_dir / "AutoPkg-Assets.pkg.zip"
            if zip_file.is_file():
                return (zip_file, "zip")

        fallback_pkg = Path(self.constants.installer_pkg_path)
        if fallback_pkg.is_file():
            return (fallback_pkg, "pkg")
        return (None, None)


    def _create_applications_symlink(self, data_root: Path) -> None:
        apps_dir = data_root / "Applications"
        if not apps_dir.exists():
            subprocess_wrapper.run_as_root(["/bin/mkdir", "-p", str(apps_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        shim_path = apps_dir / "OpenCore-Patcher.app"
        if shim_path.is_symlink():
            subprocess_wrapper.run_as_root(["/bin/rm", "-f", str(shim_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        elif shim_path.exists():
            subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(shim_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        subprocess_wrapper.run_as_root_and_verify(
            ["/bin/ln", "-s", "/Library/Application Support/Dortania/OpenCore-Patcher.app", str(shim_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


    def install_offline_app_bundle(self) -> bool:
        """
        Install OpenCore-Patcher.app (and PrivilegedHelperTool if packaged) onto
        `<target_data>` safely without executing live-host PackageKit scripts.
        """
        if not self.is_offline_target:
            return False

        asset_path, asset_type = self._find_offline_pkg_or_app()
        if asset_path is None:
            logging.info("- No offline OpenCore-Patcher.app or AutoPkg-Assets.pkg found; skipping target app install")
            return False

        data_root = self._data_root()
        dortania_dir = data_root / "Library/Application Support/Dortania"
        dest_app = dortania_dir / "OpenCore-Patcher.app"
        helper_dir = data_root / "Library/PrivilegedHelperTools"
        helper_name = "com.dortania.opencore-legacy-patcher.privileged-helper"

        subprocess_wrapper.run_as_root_and_verify(["/bin/mkdir", "-p", str(dortania_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        if asset_type == "app":
            logging.info(f"- Installing OpenCore-Patcher.app from {asset_path} to {dest_app}")
            if dest_app.exists():
                subprocess_wrapper.run_as_root_and_verify(["/bin/rm", "-rf", str(dest_app)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess_wrapper.run_as_root_and_verify(generate_copy_arguments(str(asset_path), str(dest_app)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess_wrapper.run_as_root(["/usr/sbin/chown", "-Rf", "root:wheel", str(dest_app)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self._create_applications_symlink(data_root)
            return True

        stage_dir = dortania_dir / f".ocpbp_autopkg_stage_{os.getpid()}"
        if stage_dir.exists():
            subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(stage_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        try:
            subprocess_wrapper.run_as_root_and_verify(["/bin/mkdir", "-p", str(stage_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            pkg_to_expand = asset_path
            if asset_type == "zip":
                unzip_res = subprocess_wrapper.run_as_root(
                    ["/usr/bin/ditto", "-x", "-k", "--sequesterRsrc", "--rsrc", str(asset_path), str(stage_dir)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                if unzip_res.returncode != 0:
                    logging.warning("- Failed to extract AutoPkg-Assets.pkg.zip")
                    subprocess_wrapper.log(unzip_res)
                    return False
                extracted_pkgs = sorted(stage_dir.glob("*.pkg"))
                if not extracted_pkgs:
                    logging.warning("- No .pkg found inside AutoPkg-Assets.pkg.zip")
                    return False
                pkg_to_expand = extracted_pkgs[0]

            expanded_dir = stage_dir / "expanded"
            expand_res = subprocess_wrapper.run_as_root(
                ["/usr/sbin/pkgutil", "--expand-full", str(pkg_to_expand), str(expanded_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if expand_res.returncode != 0:
                logging.warning(f"- pkgutil --expand-full failed for {pkg_to_expand}")
                subprocess_wrapper.log(expand_res)
                return False

            app_candidates = [
                p for p in expanded_dir.rglob("OpenCore-Patcher.app")
                if p.is_dir() and (p / "Contents").is_dir()
            ]
            if not app_candidates:
                logging.warning(f"- OpenCore-Patcher.app not found inside {pkg_to_expand}")
                return False

            # Pick shallowest matching OpenCore-Patcher.app bundle
            src_app = min(app_candidates, key=lambda p: len(p.parts))
            logging.info(f"- Installing OpenCore-Patcher.app to {dest_app}")
            if dest_app.exists():
                subprocess_wrapper.run_as_root_and_verify(["/bin/rm", "-rf", str(dest_app)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess_wrapper.run_as_root_and_verify(["/bin/mv", str(src_app), str(dest_app)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            subprocess_wrapper.run_as_root(["/usr/sbin/chown", "-Rf", "root:wheel", str(dest_app)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            helper_candidates = [
                p for p in expanded_dir.rglob(helper_name)
                if p.is_file()
            ]
            if helper_candidates:
                src_helper = min(helper_candidates, key=lambda p: len(p.parts))
                dest_helper = helper_dir / helper_name
                if not helper_dir.exists():
                    subprocess_wrapper.run_as_root_and_verify(["/bin/mkdir", "-p", str(helper_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if dest_helper.exists():
                    subprocess_wrapper.run_as_root_and_verify(["/bin/rm", "-f", str(dest_helper)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                subprocess_wrapper.run_as_root_and_verify(["/bin/mv", str(src_helper), str(dest_helper)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                subprocess_wrapper.run_as_root(["/usr/sbin/chown", "root:wheel", str(dest_helper)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                subprocess_wrapper.run_as_root(["/bin/chmod", "+s", str(dest_helper)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            self._create_applications_symlink(data_root)
            return True
        finally:
            if stage_dir.exists():
                subprocess_wrapper.run_as_root(["/bin/rm", "-rf", str(stage_dir)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


    def install_auto_patcher_launch_agent(self, kdk_caching_needed: bool = False):
        """
        Install patcher launch services

        See start_auto_patch() comments for more info
        """

        app_installed = False
        if self.is_offline_target:
            app_installed = self.install_offline_app_bundle()
            target_app = self._data_root() / "Library/Application Support/Dortania/OpenCore-Patcher.app"
            if self.constants.launcher_script is not None and not app_installed and not target_app.exists() and self.offline_assets_dir is None:
                logging.info("- Skipping Auto Patcher Launch Agent, not supported when running from source")
                return
        elif self.constants.launcher_script is not None:
            logging.info("- Skipping Auto Patcher Launch Agent, not supported when running from source")
            return

        data_root = self._data_root()
        temp_staging_dirs: list[Path] = []

        try:
            rsr_plist_source = self._create_rsr_monitor_daemon(temp_staging_dirs)
            services = {
                Path(self.constants.auto_patch_launch_agent_path):        str(data_root / "Library/LaunchAgents/com.dortania.opencore-legacy-patcher.auto-patch.plist"),
                Path(self.constants.update_launch_daemon_path):           str(data_root / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.macos-update.plist"),
                **({ rsr_plist_source: str(data_root / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.rsr-monitor.plist") } if rsr_plist_source else {}),
                **({ Path(self.constants.kdk_launch_daemon_path):         str(data_root / "Library/LaunchDaemons/com.dortania.opencore-legacy-patcher.os-caching.plist") } if kdk_caching_needed is True else {} ),
            }

            for service, dest in services.items():
                service_path = Path(service)
                dest_path = Path(dest)
                if not service_path.exists():
                    logging.warning(f"- Launch service template missing: {service_path}")
                    continue

                name = service_path.name
                logging.info(f"- Installing {name}")
                if dest_path.exists():
                    with service_path.open("rb") as src_f, dest_path.open("rb") as dst_f:
                        if hashlib.sha256(src_f.read()).hexdigest() == hashlib.sha256(dst_f.read()).hexdigest():
                            logging.info(f"  - {name} checksums match, skipping")
                            continue
                    logging.info("  - Existing service found, removing")
                    subprocess_wrapper.run_as_root_and_verify(["/bin/rm", str(dest_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                # Create parent directories
                if not dest_path.parent.exists():
                    logging.info(f"  - Creating {dest_path.parent} directory")
                    subprocess_wrapper.run_as_root_and_verify(["/bin/mkdir", "-p", str(dest_path.parent)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                subprocess_wrapper.run_as_root_and_verify(generate_copy_arguments(str(service_path), str(dest_path)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                # Set the permissions on the service
                subprocess_wrapper.run_as_root_and_verify(["/bin/chmod", "644", str(dest_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                subprocess_wrapper.run_as_root_and_verify(["/usr/sbin/chown", "root:wheel", str(dest_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        finally:
            for temp_dir in temp_staging_dirs:
                try:
                    for item in temp_dir.iterdir():
                        item.unlink()
                    temp_dir.rmdir()
                except OSError:
                    pass


    def _resolve_target_cryptex_paths(self) -> Optional[str]:
        """
        Resolve runtime `/System/Volumes/Preboot/<UUID>/cryptex1/current/OS.dmg`
        path when target `preboot_mount` has `cryptex1/current/OS.dmg`.
        """
        if not self.is_offline_target:
            try:
                cryptex_path = f"/System/Volumes/Preboot/{utilities.get_preboot_uuid()}/cryptex1/current/OS.dmg"
                return cryptex_path if Path(cryptex_path).exists() else None
            except Exception:
                return None

        if not self.preboot_mount or not self.preboot_mount.is_dir():
            return None

        direct = self.preboot_mount / "cryptex1/current/OS.dmg"
        if direct.exists():
            return f"/System/Volumes/Preboot/{self.preboot_mount.name}/cryptex1/current/OS.dmg"

        try:
            for sub in sorted(self.preboot_mount.iterdir()):
                if sub.is_dir() and (sub / "cryptex1/current/OS.dmg").exists():
                    return f"/System/Volumes/Preboot/{sub.name}/cryptex1/current/OS.dmg"
        except OSError:
            pass
        return None


    def _create_rsr_monitor_daemon(self, temp_staging_dirs: Optional[list[Path]] = None) -> Optional[Path]:
        # Get kext list in /Library/Extensions that have the 'GPUCompanionBundles' property
        # This is used to determine if we need to run the RSRMonitor
        logging.info("- Checking if RSRMonitor is needed")

        cryptex_path = self._resolve_target_cryptex_paths()
        if not cryptex_path:
            logging.info("- No OS.dmg, skipping RSRMonitor")
            return None

        extensions_dir = self._data_root() / "Library/Extensions"
        if not extensions_dir.is_dir():
            logging.info("- No Library/Extensions directory, skipping RSRMonitor")
            return None

        kexts = []
        for kext in extensions_dir.glob("*.kext"):
            info_plist = kext / "Contents/Info.plist"
            try:
                if not info_plist.exists():
                    continue
            except Exception as e:
                logging.info(f"  - Failed to check if {kext.name} is a directory: {e}")
                continue
            try:
                with info_plist.open("rb") as f:
                    kext_plist = plistlib.load(f)
            except Exception as e:
                logging.info(f"  - Failed to load plist for {kext.name}: {e}")
                continue
            if "GPUCompanionBundles" not in kext_plist:
                continue
            logging.info(f"  - Found kext with GPUCompanionBundles: {kext.name}")
            kexts.append(kext.name)

        # If we have no kexts, we don't need to run the RSRMonitor
        if not kexts:
            logging.info("- No kexts found with GPUCompanionBundles, skipping RSRMonitor")
            return None

        # Load the RSRMonitor plist template
        template_path = Path(self.constants.rsr_monitor_launch_daemon_path)
        with template_path.open("rb") as f:
            rsr_monitor_plist = plistlib.load(f)

        arguments = ["/bin/rm", "-Rfv"]
        arguments += [f"/Library/Extensions/{kext}" for kext in kexts]

        # Add the arguments to the RSRMonitor plist
        rsr_monitor_plist["ProgramArguments"] = arguments

        # Next add monitoring for '/System/Volumes/Preboot/{UUID}/cryptex1/current/OS.dmg'
        logging.info(f"  - Adding monitor: {cryptex_path}")
        rsr_monitor_plist["WatchPaths"] = [
            cryptex_path,
        ]

        # Write to a writable temp file if offline or payload path is read-only
        if not self.is_offline_target and os.access(template_path, os.W_OK):
            dest_template = template_path
        else:
            stage = Path(tempfile.mkdtemp(prefix="ocpbp_rsrmon_"))
            if temp_staging_dirs is not None:
                temp_staging_dirs.append(stage)
            dest_template = stage / template_path.name

        with dest_template.open("wb") as f:
            plistlib.dump(rsr_monitor_plist, f)

        return dest_template

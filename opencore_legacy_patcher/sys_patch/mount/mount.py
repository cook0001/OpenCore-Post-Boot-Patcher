"""
mount.py: Handling macOS root volume mounting and unmounting
"""

import getpass
import logging
import plistlib
import re
import shutil
import subprocess
import tempfile

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .snapshot import APFSSnapshot

from ...datasets import os_data
from ...support  import subprocess_wrapper


def _strip_snapshot_suffix(device_identifier: str) -> str:
    """
    Strip APFS snapshot suffix from a volume device identifier.

    ex. disk3s1s1 -> disk3s1
    """
    return re.sub(r"^(disk\d+s\d+)s\d+$", r"\1", device_identifier)


@dataclass
class TargetVolumeGroup:
    container_disk: str
    system_identifier: str
    volume_name: str
    data_identifier: Optional[str] = None
    data_volume_name: Optional[str] = None
    preboot_identifier: Optional[str] = None
    system_volume_uuid: Optional[str] = None
    data_volume_uuid: Optional[str] = None
    volume_group_uuid: Optional[str] = None
    system_mount: Optional[str] = None
    data_mount: Optional[str] = None
    preboot_mount: Optional[str] = None
    filevault_enabled: bool = False
    data_locked: bool = False
    is_live_booted: bool = False
    physical_stores: list[str] = field(default_factory=list)


class APFSVolumeResolver:
    """
    Discover and resolve macOS APFS volume groups (System + Data + Preboot).
    """

    @staticmethod
    def _run_diskutil_plist(args: list[str]) -> Optional[dict]:
        try:
            result = subprocess.run(
                ["/usr/sbin/diskutil"] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            return plistlib.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, TypeError):
            return None

    def diskutil_info(self, target: str) -> Optional[dict]:
        return self._run_diskutil_plist(["info", "-plist", str(target)])

    def _container_physical_stores(self, container: dict) -> list[str]:
        stores: list[str] = []
        for store in container.get("PhysicalStores", []):
            dev = store.get("DeviceIdentifier")
            if dev and dev not in stores:
                stores.append(dev)
        if stores:
            return stores

        container_ref = container.get("ContainerReference")
        if container_ref:
            info = self.diskutil_info(container_ref)
            if info:
                for entry in info.get("APFSPhysicalStores", []):
                    dev = entry.get("APFSPhysicalStore")
                    if dev and dev not in stores:
                        stores.append(dev)
        return stores

    @staticmethod
    def _volume_group_map_from_container(container: dict) -> dict[str, str]:
        """
        Map APFS DeviceIdentifier / VolumeUUID -> VolumeGroupUUID when
        VolumeGroups is exposed in `diskutil apfs list -plist`.
        """
        mapping: dict[str, str] = {}
        for group in container.get("VolumeGroups", []):
            group_uuid = group.get("VolumeGroupUUID") or group.get("APFSVolumeGroupUUID")
            if not group_uuid:
                continue
            for member in group.get("Volumes", []):
                dev = member.get("DeviceIdentifier")
                vol_uuid = member.get("APFSVolumeUUID") or member.get("VolumeUUID")
                if dev:
                    mapping[_strip_snapshot_suffix(dev)] = group_uuid
                if vol_uuid:
                    mapping[vol_uuid] = group_uuid
        return mapping

    def discover_volume_groups(self, include_live_root: bool = False) -> list[TargetVolumeGroup]:
        apfs_plist = self._run_diskutil_plist(["apfs", "list", "-plist"])
        if not apfs_plist:
            return []

        groups: list[TargetVolumeGroup] = []
        for container in apfs_plist.get("Containers", []):
            container_disk = container.get("ContainerReference", "")
            volumes = container.get("Volumes", [])
            if not volumes:
                continue

            physical_stores = self._container_physical_stores(container)
            group_map = self._volume_group_map_from_container(container)

            enriched_vols: list[dict] = []
            preboot_vol: Optional[dict] = None
            data_vols: list[dict] = []
            system_vols: list[dict] = []

            for vol in volumes:
                raw_dev = vol.get("DeviceIdentifier", "")
                if not raw_dev:
                    continue
                base_dev = _strip_snapshot_suffix(raw_dev)
                info = self.diskutil_info(raw_dev) or {}

                roles = list(vol.get("Roles") or info.get("Roles") or [])
                name = vol.get("Name") or info.get("VolumeName") or ""
                vol_uuid = vol.get("APFSVolumeUUID") or vol.get("VolumeUUID") or info.get("VolumeUUID")
                mount_point = vol.get("MountPoint") or info.get("MountPoint")
                if mount_point == "":
                    mount_point = None

                vg_uuid = (
                    vol.get("VolumeGroupUUID")
                    or vol.get("APFSVolumeGroupUUID")
                    or info.get("APFSVolumeGroupID")
                    or group_map.get(base_dev)
                    or (group_map.get(vol_uuid) if vol_uuid else None)
                )

                encrypted = bool(
                    vol.get("FileVault")
                    or vol.get("Encryption")
                    or info.get("FileVault")
                    or info.get("Encryption")
                )
                locked = bool(vol.get("Locked") or info.get("Locked"))

                entry = {
                    "raw_dev": raw_dev,
                    "base_dev": base_dev,
                    "roles": roles,
                    "name": name,
                    "vol_uuid": vol_uuid,
                    "vg_uuid": vg_uuid,
                    "mount_point": mount_point,
                    "encrypted": encrypted,
                    "locked": locked,
                }
                enriched_vols.append(entry)

                if "Preboot" in roles and preboot_vol is None:
                    preboot_vol = entry
                if "Data" in roles:
                    data_vols.append(entry)
                if "System" in roles:
                    system_vols.append(entry)

            for sys_vol in system_vols:
                sys_mount = sys_vol["mount_point"]
                sys_name = sys_vol["name"]
                is_live = sys_mount == "/"

                if not include_live_root:
                    if is_live:
                        continue
                    if sys_mount and (
                        sys_mount.startswith("/System/Volumes/BaseSystem")
                        or sys_mount.startswith("/System/Volumes/Update")
                    ):
                        continue
                    if sys_name in ("OS X Base System", "macOS Base System", "Update", "Recovery"):
                        continue

                if sys_mount and Path(sys_mount).exists():
                    sys_ver = Path(sys_mount) / "System/Library/CoreServices/SystemVersion.plist"
                    if not sys_ver.exists():
                        continue

                paired_data = self._match_data_volume(sys_vol, data_vols, len(system_vols))
                groups.append(
                    TargetVolumeGroup(
                        container_disk=container_disk,
                        system_identifier=sys_vol["base_dev"],
                        volume_name=sys_name or sys_vol["base_dev"],
                        data_identifier=paired_data["base_dev"] if paired_data else None,
                        data_volume_name=paired_data["name"] if paired_data else None,
                        preboot_identifier=preboot_vol["base_dev"] if preboot_vol else None,
                        system_volume_uuid=sys_vol["vol_uuid"],
                        data_volume_uuid=paired_data["vol_uuid"] if paired_data else None,
                        volume_group_uuid=sys_vol["vg_uuid"] or (paired_data["vg_uuid"] if paired_data else None),
                        system_mount=sys_mount,
                        data_mount=paired_data["mount_point"] if paired_data else None,
                        preboot_mount=preboot_vol["mount_point"] if preboot_vol else None,
                        filevault_enabled=bool(paired_data["encrypted"]) if paired_data else bool(sys_vol["encrypted"]),
                        data_locked=bool(paired_data["locked"]) if paired_data else False,
                        is_live_booted=is_live,
                        physical_stores=list(physical_stores),
                    )
                )

        return groups

    @staticmethod
    def _match_data_volume(sys_vol: dict, data_vols: list[dict], system_count: int) -> Optional[dict]:
        sys_vg = sys_vol.get("vg_uuid")
        if sys_vg:
            for data_vol in data_vols:
                if data_vol.get("vg_uuid") == sys_vg:
                    return data_vol

        sys_name = sys_vol.get("name") or ""
        if sys_name:
            expected_data_name = f"{sys_name} - Data"
            for data_vol in data_vols:
                if data_vol.get("name") == expected_data_name:
                    return data_vol

        if system_count == 1 and len(data_vols) == 1:
            return data_vols[0]

        return None

    def resolve_target(self, target: str) -> TargetVolumeGroup:
        raw_target = str(target).strip()
        if not raw_target:
            raise RuntimeError("Target volume cannot be empty.")

        normalized_path = raw_target
        if raw_target != "/" and raw_target.endswith("/"):
            normalized_path = raw_target.rstrip("/")

        resolved_path = None
        if Path(normalized_path).exists() and not normalized_path.startswith("/dev/"):
            try:
                resolved_path = str(Path(normalized_path).resolve())
            except OSError:
                resolved_path = normalized_path

        stripped_dev = _strip_snapshot_suffix(normalized_path.removeprefix("/dev/"))
        groups = self.discover_volume_groups(include_live_root=True)

        for group in groups:
            mount_candidates = {m for m in (group.system_mount, group.data_mount) if m}
            if normalized_path in mount_candidates or (resolved_path and resolved_path in mount_candidates):
                return group

        for group in groups:
            dev_candidates = {d for d in (group.system_identifier, group.data_identifier) if d}
            if stripped_dev in dev_candidates:
                return group

        upper_target = raw_target.upper()
        for group in groups:
            uuid_candidates = {
                u.upper()
                for u in (group.system_volume_uuid, group.data_volume_uuid, group.volume_group_uuid)
                if u
            }
            if upper_target in uuid_candidates:
                return group

        name_matches = [
            group for group in groups
            if raw_target in (group.volume_name, group.data_volume_name)
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            ids = ", ".join(g.system_identifier for g in name_matches)
            raise RuntimeError(
                f"Multiple APFS volumes named '{raw_target}' found ({ids}). Specify a mount path or disk identifier."
            )

        info = self.diskutil_info(raw_target)
        if info:
            info_dev = _strip_snapshot_suffix(info.get("DeviceIdentifier", ""))
            info_vg = info.get("APFSVolumeGroupID")
            for group in groups:
                if info_dev and info_dev in (group.system_identifier, group.data_identifier):
                    return group
                if info_vg and group.volume_group_uuid == info_vg:
                    return group

        raise RuntimeError(f"Unable to resolve APFS target volume group for: {target}")

    def unlock_data_volume(
        self,
        group: TargetVolumeGroup,
        passphrase: Optional[str] = None,
        interactive: bool = False,
    ) -> TargetVolumeGroup:
        if not group.data_locked or not group.data_identifier:
            return group

        secret = passphrase
        if secret is None and interactive:
            label = group.data_volume_name or group.data_identifier
            secret = getpass.getpass(f"Enter FileVault password or recovery key for '{label}': ")

        if secret is None:
            label = group.data_volume_name or group.data_identifier
            raise RuntimeError(
                f"Target Data volume '{label}' is FileVault-locked. Unlock it first or provide a passphrase."
            )

        result = subprocess_wrapper.run_as_root(
            ["/usr/sbin/diskutil", "apfs", "unlockVolume", group.data_identifier, "-stdinpassphrase"],
            input=secret.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode != 0:
            subprocess_wrapper.log(result)
            raise RuntimeError(f"Failed to unlock FileVault Data volume {group.data_identifier}")

        refreshed = self.diskutil_info(group.data_identifier) or {}
        group.data_locked = bool(refreshed.get("Locked", False))
        mount_point = refreshed.get("MountPoint")
        if mount_point:
            group.data_mount = mount_point
        return group


class RootVolumeMount:

    def __init__(
        self,
        xnu_major: int,
        target_root: Optional[Union[str, Path]] = None,
        data_root: Optional[Union[str, Path]] = None,
        preboot_root: Optional[Union[str, Path]] = None,
        passphrase: Optional[str] = None,
        interactive_unlock: bool = False,
    ) -> None:
        self.xnu_major = xnu_major
        self.target_root = str(target_root) if target_root else None
        self.data_root = str(data_root) if data_root else None
        self.preboot_root = str(preboot_root) if preboot_root else None
        self.passphrase = passphrase
        self.interactive_unlock = interactive_unlock

        self.resolver = APFSVolumeResolver()
        self.volume_group: Optional[TargetVolumeGroup] = None
        self.is_offline_target: bool = False

        self.mount_path: Optional[str] = None
        self.system_mount: Optional[str] = None
        self.data_mount: Optional[str] = None
        self.preboot_mount: Optional[str] = None

        self._temp_mount_dirs: list[Path] = []
        self._mounted_system_by_us: bool = False
        self._mounted_data_by_us: bool = False
        self._mounted_preboot_by_us: bool = False

        if self.target_root and self.target_root != "/":
            self.volume_group = self.resolver.resolve_target(self.target_root)
            if self.volume_group.is_live_booted:
                self.root_volume_identifier = self.volume_group.system_identifier
            else:
                self.is_offline_target = True
                self.root_volume_identifier = self.volume_group.system_identifier
                self.data_mount = self.data_root or self.volume_group.data_mount
                self.preboot_mount = self.preboot_root or self.volume_group.preboot_mount
        else:
            self.root_volume_identifier = self._fetch_root_volume_identifier()


    def _fetch_root_volume_identifier(self) -> str:
        """
        Resolve path to disk identifier

        ex. / -> disk1s1
        """
        try:
            content = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "info", "-plist", "/"], capture_output=True).stdout)
        except plistlib.InvalidFileException:
            raise RuntimeError("Failed to parse diskutil output.")

        disk = content["DeviceIdentifier"]

        if "APFSSnapshot" in content and content["APFSSnapshot"] is True:
            disk = _strip_snapshot_suffix(disk)

        return disk


    def _create_staging_mount_dir(self, label: str) -> Path:
        staging_dir = Path(tempfile.mkdtemp(prefix=f"ocpbp_{label}_"))
        self._temp_mount_dirs.append(staging_dir)
        return staging_dir


    def _mount_apfs_device_rw(self, device_identifier: str, mount_point: Path) -> bool:
        mount_point.mkdir(parents=True, exist_ok=True)
        result = subprocess_wrapper.run_as_root(
            ["/sbin/mount", "-o", "nobrowse", "-t", "apfs", f"/dev/{device_identifier}", str(mount_point)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode == 0:
            return True

        logging.warning(f"/sbin/mount failed for /dev/{device_identifier}, retrying with /sbin/mount_apfs")
        subprocess_wrapper.log(result)
        fallback = subprocess_wrapper.run_as_root(
            ["/sbin/mount_apfs", "-o", "nobrowse", f"/dev/{device_identifier}", str(mount_point)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if fallback.returncode != 0:
            logging.error(f"Failed to mount APFS device /dev/{device_identifier} at {mount_point}")
            subprocess_wrapper.log(fallback)
            return False
        return True


    def _ensure_offline_companion_mounts(self) -> bool:
        if not self.volume_group:
            return False

        # Ensure Data volume is unlocked and mounted
        if self.volume_group.data_identifier:
            if self.volume_group.data_locked:
                try:
                    self.volume_group = self.resolver.unlock_data_volume(
                        self.volume_group,
                        passphrase=self.passphrase,
                        interactive=self.interactive_unlock,
                    )
                    self.data_mount = self.data_root or self.volume_group.data_mount
                    self.preboot_mount = self.preboot_root or self.volume_group.preboot_mount
                except RuntimeError as err:
                    logging.error(str(err))
                    return False

            if not self.data_mount or not Path(self.data_mount).exists():
                info = self.resolver.diskutil_info(self.volume_group.data_identifier) or {}
                existing = info.get("MountPoint")
                if existing and Path(existing).exists():
                    self.data_mount = existing
                else:
                    mount_res = subprocess_wrapper.run_as_root(
                        ["/usr/sbin/diskutil", "mount", self.volume_group.data_identifier],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    refreshed = self.resolver.diskutil_info(self.volume_group.data_identifier) or {}
                    if mount_res.returncode == 0 and refreshed.get("MountPoint"):
                        self.data_mount = refreshed["MountPoint"]
                        self._mounted_data_by_us = True
                    else:
                        data_stage = self._create_staging_mount_dir(self.volume_group.data_identifier)
                        if not self._mount_apfs_device_rw(self.volume_group.data_identifier, data_stage):
                            return False
                        self.data_mount = str(data_stage)
                        self._mounted_data_by_us = True

        # Ensure Preboot volume is mounted when present
        if self.volume_group.preboot_identifier and (not self.preboot_mount or not Path(self.preboot_mount).exists()):
            info = self.resolver.diskutil_info(self.volume_group.preboot_identifier) or {}
            existing = info.get("MountPoint")
            if existing and Path(existing).exists():
                self.preboot_mount = existing
            else:
                preboot_stage = self._create_staging_mount_dir(self.volume_group.preboot_identifier)
                if self._mount_apfs_device_rw(self.volume_group.preboot_identifier, preboot_stage):
                    self.preboot_mount = str(preboot_stage)
                    self._mounted_preboot_by_us = True

        return True


    def _mount_offline_root_volume(self) -> Optional[str]:
        if not self._ensure_offline_companion_mounts():
            return None

        sys_id = self.root_volume_identifier
        sys_info = self.resolver.diskutil_info(sys_id) or {}
        existing_mount = sys_info.get("MountPoint")
        is_snapshot = bool(sys_info.get("APFSSnapshot", False))

        # If base System volume (not snapshot) is already mounted elsewhere, unmount before RW staging
        if existing_mount and not is_snapshot and existing_mount != "/":
            if sys_info.get("WritableVolume") is True and Path(existing_mount, "System/Library/CoreServices/SystemVersion.plist").exists():
                self.system_mount = existing_mount
                return existing_mount

            unmount_res = subprocess_wrapper.run_as_root(
                ["/usr/sbin/diskutil", "unmount", sys_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if unmount_res.returncode != 0:
                logging.error(f"Failed to unmount existing read-only mount for {sys_id} at {existing_mount}")
                subprocess_wrapper.log(unmount_res)
                return None

        staging_mount = self._create_staging_mount_dir(sys_id)
        if not self._mount_apfs_device_rw(sys_id, staging_mount):
            return None

        self._mounted_system_by_us = True
        self.system_mount = str(staging_mount)
        return self.system_mount


    def _mount_root_volume(self) -> Optional[str]:
        """
        Mount the root volume.

        Returns the path to the root volume.
        """
        if self.is_offline_target:
            return self._mount_offline_root_volume()

        # Root volume same as data volume
        if self.xnu_major < os_data.os_data.catalina.value:
            self.system_mount = "/"
            return "/"

        # Catalina implemented a read-only root volume
        if self.xnu_major == os_data.os_data.catalina.value:
            result = subprocess_wrapper.run_as_root(["/sbin/mount", "-uw", "/"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                logging.error("Failed to mount root volume")
                subprocess_wrapper.log(result)
                return None
            self.system_mount = "/"
            return "/"

        # Big Sur and newer implemented APFS snapshots for the root volume
        if self.xnu_major >= os_data.os_data.big_sur.value:
            if Path("/System/Volumes/Update/mnt1/System/Library/CoreServices/SystemVersion.plist").exists():
                self.system_mount = "/System/Volumes/Update/mnt1"
                return "/System/Volumes/Update/mnt1"
            result = subprocess_wrapper.run_as_root(["/sbin/mount", "-o", "nobrowse", "-t", "apfs", f"/dev/{self.root_volume_identifier}", "/System/Volumes/Update/mnt1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                logging.error("Failed to mount root volume")
                subprocess_wrapper.log(result)
                return None
            self._mounted_system_by_us = True
            self.system_mount = "/System/Volumes/Update/mnt1"
            return "/System/Volumes/Update/mnt1"

        return None


    def _unmount_path(self, path: str, ignore_errors: bool = True, extra_args: Optional[list[str]] = None) -> bool:
        args = ["/sbin/umount"]
        if extra_args:
            args += extra_args
        args.append(path)

        result = subprocess_wrapper.run_as_root(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            if ignore_errors is False:
                logging.error(f"Failed to unmount volume at {path}")
                subprocess_wrapper.log(result)
            return False
        return True


    def _cleanup_temp_mount_dirs(self) -> None:
        for temp_dir in self._temp_mount_dirs:
            try:
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
            except OSError:
                pass
        self._temp_mount_dirs.clear()


    def _unmount_root_volume(self, ignore_errors: bool = True) -> bool:
        """
        Unmount the root volume.
        """
        if self.is_offline_target:
            ok = True
            if self._mounted_system_by_us and self.mount_path:
                ok = self._unmount_path(self.mount_path, ignore_errors=ignore_errors) and ok
                if ok:
                    self._mounted_system_by_us = False
            if self._mounted_preboot_by_us and self.preboot_mount:
                ok = self._unmount_path(self.preboot_mount, ignore_errors=ignore_errors) and ok
                if ok:
                    self._mounted_preboot_by_us = False
            if self._mounted_data_by_us and self.data_mount:
                ok = self._unmount_path(self.data_mount, ignore_errors=ignore_errors) and ok
                if ok:
                    self._mounted_data_by_us = False
            self._cleanup_temp_mount_dirs()
            return ok

        if self.xnu_major < os_data.os_data.catalina.value:
            return True

        extra_args = ["-uw"] if self.xnu_major == os_data.os_data.catalina.value else None
        return self._unmount_path(self.mount_path, ignore_errors=ignore_errors, extra_args=extra_args)


    def mount(self) -> Optional[str]:
        """
        Mount the root volume.

        Returns the path to the root volume.

        If none, failed to mount.
        """
        result = self._mount_root_volume()
        if result is None:
            logging.error("Failed to mount root volume")
            return None
        if not Path(result).exists():
            logging.error(f"Attempted to mount root volume, but failed: {result}")
            return None

        self.mount_path = result

        return result


    def unmount(self, ignore_errors: bool = True) -> bool:
        """
        Unmount the root volume.

        Returns True if successful, False otherwise.

        Note for Big Sur and newer, a snapshot is created before unmounting.
        And that unmounting is not critical to the process.
        """
        return self._unmount_root_volume(ignore_errors=ignore_errors)


    def create_snapshot(self) -> bool:
        """
        Create APFS snapshot of the root volume.
        """
        return APFSSnapshot(self.xnu_major, self.mount_path).create_snapshot()


    def revert_snapshot(self) -> bool:
        """
        Revert APFS snapshot of the root volume.
        """
        return APFSSnapshot(self.xnu_major, self.mount_path).revert_snapshot()
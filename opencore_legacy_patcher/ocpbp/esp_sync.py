"""
esp_sync.py: Offline asset discovery, root-patch EFI validation/enforcement,
and one-step internal ESP OpenCore synchronization for Recovery/OCPBP workflows.
"""

import binascii
import copy
import logging
import plistlib
import re
import shutil
import subprocess
import tempfile

from pathlib import Path
from typing import Optional, Union

from .. import constants
from ..datasets import model_array
from ..detections import device_probe
from ..efi_builder import build
from ..support import defaults, install, subprocess_wrapper, utilities
from ..sys_patch.mount import mount


APPLE_NVRAM_GUID = "7C436110-AB2A-4BBB-A880-FE41995C9F82"
OCLP_NVRAM_GUID = "4D1FDA02-38C7-4A6A-9CC6-4BCCA8B30102"

CSR_ALLOW_UNTRUSTED_KEXTS = 0x1
CSR_ALLOW_UNRESTRICTED_FS = 0x2
CSR_ALLOW_UNAUTHENTICATED_ROOT = 0x800
REQUIRED_ROOT_PATCH_CSR_MASK = (
    CSR_ALLOW_UNTRUSTED_KEXTS
    | CSR_ALLOW_UNRESTRICTED_FS
    | CSR_ALLOW_UNAUTHENTICATED_ROOT
)
ROOT_PATCH_CSR_BYTES = binascii.unhexlify("03080000")

_OFFLINE_ASSET_MARKERS = (
    "Universal-Binaries.dmg",
    "AutoPkg-Assets.pkg",
    "KDK.pkg",
    "KDK.dmg",
    "MetallibSupportPkg.pkg",
    ".ocpbp-manifest.plist",
)


def discover_offline_assets_dir(
    explicit_dir: Optional[Union[str, Path]] = None,
    global_constants: Optional[constants.Constants] = None,
) -> Optional[Path]:
    """
    Resolve the directory storing offline root-patching payloads.

    Resolution priority:
        1. Explicitly supplied --offline_assets_dir
        2. Already configured constants.offline_assets_dir
        3. Directory containing the launched binary if OCPBP assets live beside it
        4. Mounted /Volumes/OCPBP volume when present
    """
    if explicit_dir:
        candidate = Path(explicit_dir).expanduser()
        if not candidate.is_dir():
            raise FileNotFoundError(f"Offline assets directory does not exist: {candidate}")
        return candidate

    if global_constants and getattr(global_constants, "offline_assets_dir", None):
        configured = Path(global_constants.offline_assets_dir)
        if configured.is_dir():
            return configured

    candidate_dirs: list[Path] = []
    if global_constants and getattr(global_constants, "launcher_binary", None):
        try:
            candidate_dirs.append(Path(global_constants.launcher_binary).resolve().parent)
        except OSError:
            pass
    if global_constants and getattr(global_constants, "original_path", None):
        candidate_dirs.append(Path(global_constants.original_path))

    for directory in candidate_dirs:
        if not directory.is_dir():
            continue
        if any((directory / marker).exists() for marker in _OFFLINE_ASSET_MARKERS):
            return directory

    ocpbp_mount = (
        Path(getattr(global_constants, "ocpbp_volume_mount", "/Volumes/OCPBP"))
        if global_constants
        else Path("/Volumes/OCPBP")
    )
    if ocpbp_mount.is_dir():
        return ocpbp_mount

    return None


def summarize_offline_assets(offline_dir: Optional[Union[str, Path]]) -> dict[str, Optional[Path]]:
    """
    Inspect an offline assets directory and report located payloads.
    """
    summary: dict[str, Optional[Path]] = {
        "universal_binaries": None,
        "autopkg_assets": None,
        "kdk": None,
        "metallib": None,
        "manifest": None,
    }
    if not offline_dir:
        return summary

    root = Path(offline_dir)
    if not root.is_dir():
        return summary

    if (root / "Universal-Binaries.dmg").is_file():
        summary["universal_binaries"] = root / "Universal-Binaries.dmg"

    for pkg_name in ("AutoPkg-Assets.pkg", "OpenCore-Patcher.pkg"):
        if (root / pkg_name).is_file():
            summary["autopkg_assets"] = root / pkg_name
            break

    for kdk_name in ("KDK.pkg", "KDK.dmg"):
        if (root / kdk_name).is_file():
            summary["kdk"] = root / kdk_name
            break
    if summary["kdk"] is None:
        kdk_bundles = sorted(root.glob("KDK_*.kdk"))
        if kdk_bundles:
            summary["kdk"] = kdk_bundles[0]

    if (root / "MetallibSupportPkg.pkg").is_file():
        summary["metallib"] = root / "MetallibSupportPkg.pkg"

    if (root / ".ocpbp-manifest.plist").is_file():
        summary["manifest"] = root / ".ocpbp-manifest.plist"

    return summary


def _parse_csr_value(raw_csr: object) -> int:
    if isinstance(raw_csr, (bytes, bytearray)) and len(raw_csr) > 0:
        return int.from_bytes(raw_csr[:4], "little")
    if isinstance(raw_csr, int):
        return raw_csr
    if isinstance(raw_csr, str):
        cleaned = raw_csr.strip().lower().removeprefix("0x")
        if cleaned:
            try:
                return int.from_bytes(binascii.unhexlify(cleaned.zfill(8)), "little")
            except (ValueError, binascii.Error):
                return 0
    return 0


def _parse_custom_sip_int(custom_sip: object) -> Optional[int]:
    if custom_sip is None or custom_sip == "":
        return None
    if isinstance(custom_sip, int):
        return custom_sip
    if isinstance(custom_sip, str):
        try:
            return int(custom_sip.strip(), 16)
        except ValueError:
            return None
    return None


def inspect_efi_config(config_or_path: Union[dict, str, Path]) -> dict:
    """
    Validate whether an OpenCore config.plist enforces SIP/AR bits and
    SecureBootModel=Disabled for root volume patching.
    """
    if isinstance(config_or_path, (str, Path)):
        with Path(config_or_path).open("rb") as handle:
            config = plistlib.load(handle)
    else:
        config = config_or_path

    nvram_add = config.get("NVRAM", {}).get("Add", {})
    apple_nvram = nvram_add.get(APPLE_NVRAM_GUID, {})
    oclp_nvram = nvram_add.get(OCLP_NVRAM_GUID, {})
    nvram_delete = config.get("NVRAM", {}).get("Delete", {}).get(APPLE_NVRAM_GUID, [])

    csr_bytes = apple_nvram.get("csr-active-config", b"")
    csr_int = _parse_csr_value(csr_bytes)
    sip_allows_root_patch = (csr_int & REQUIRED_ROOT_PATCH_CSR_MASK) == REQUIRED_ROOT_PATCH_CSR_MASK

    secure_boot_model = config.get("Misc", {}).get("Security", {}).get("SecureBootModel")
    secure_boot_disabled = secure_boot_model == "Disabled"

    oclp_settings = str(oclp_nvram.get("OCLP-Settings", ""))
    allow_fv_enabled = "-allow_fv" in oclp_settings
    nvram_deletes_csr = "csr-active-config" in nvram_delete

    return {
        "csr_active_config_int": csr_int,
        "csr_active_config_hex": f"0x{csr_int:08X}",
        "sip_allows_root_patch": sip_allows_root_patch,
        "secure_boot_model": secure_boot_model,
        "secure_boot_disabled": secure_boot_disabled,
        "allow_fv_enabled": allow_fv_enabled,
        "nvram_deletes_csr": nvram_deletes_csr,
        "ready_for_root_patch": bool(sip_allows_root_patch and secure_boot_disabled),
    }


def get_live_boot_security_status() -> dict:
    """
    Query active macOS/Recovery boot SIP, SecureBootModel, and FileVault seal status.
    Returns None for metrics unavailable on non-macOS test environments.
    """
    probe_errors = (ImportError, OSError, RuntimeError, subprocess.SubprocessError, AttributeError)
    csr_int: Optional[int] = None
    try:
        import py_sip_xnu  # type: ignore
        csr_int = int(py_sip_xnu.SipXnu().get_sip_status().value)
    except probe_errors:
        try:
            raw_csr = utilities.get_nvram("csr-active-config", APPLE_NVRAM_GUID, decode=False)
            if raw_csr is not None:
                csr_int = _parse_csr_value(raw_csr)
        except probe_errors:
            csr_int = None

    sip_ready = bool(csr_int is not None and (csr_int & REQUIRED_ROOT_PATCH_CSR_MASK) == REQUIRED_ROOT_PATCH_CSR_MASK)

    sbm_active: Optional[bool]
    try:
        sbm_active = bool(utilities.check_secure_boot_level())
    except probe_errors:
        sbm_active = None

    allow_fv: Optional[bool]
    try:
        allow_fv = bool(utilities.check_filevault_skip())
    except probe_errors:
        allow_fv = None

    return {
        "csr_active_config_int": csr_int,
        "csr_active_config_hex": f"0x{csr_int:08X}" if csr_int is not None else "Unknown",
        "sip_allows_root_patch": sip_ready,
        "secure_boot_active": sbm_active,
        "allow_fv_nvram": allow_fv,
        "ready_for_root_patch": bool(sip_ready and sbm_active is False),
    }


def enforce_constants_root_patch_flags(global_constants: constants.Constants) -> None:
    """
    Ensure Constants settings allow root volume patching on OpenCore boot
    (SIP lowered with 0x803 bits + SecureBootModel=Disabled).
    """
    global_constants.sip_status = False
    global_constants.secure_status = False
    if global_constants.custom_sip_value:
        custom_val = _parse_custom_sip_int(global_constants.custom_sip_value)
        if custom_val is None or (custom_val & REQUIRED_ROOT_PATCH_CSR_MASK) != REQUIRED_ROOT_PATCH_CSR_MASK:
            logging.info("Resetting custom SIP override so required root-patch bits (0x803) are enabled")
            global_constants.custom_sip_value = None


def enforce_efi_config_flags(config_plist_path: Union[str, Path]) -> dict:
    """
    Update an existing EFI/OC/config.plist in-place so csr-active-config (0x803+),
    SecureBootModel=Disabled, and -allow_fv are enforced independently of internal NVRAM.
    """
    plist_file = Path(config_plist_path)
    with plist_file.open("rb") as handle:
        config = plistlib.load(handle)

    nvram = config.setdefault("NVRAM", {})
    nvram_add = nvram.setdefault("Add", {})
    apple_add = nvram_add.setdefault(APPLE_NVRAM_GUID, {})
    oclp_add = nvram_add.setdefault(OCLP_NVRAM_GUID, {})

    existing_csr = _parse_csr_value(apple_add.get("csr-active-config", b""))
    enforced_csr = existing_csr | REQUIRED_ROOT_PATCH_CSR_MASK
    apple_add["csr-active-config"] = enforced_csr.to_bytes(4, "little")

    boot_args = str(apple_add.get("boot-args", ""))
    for flag in ("ipc_control_port_options=0", "-nokcmismatchpanic"):
        if flag not in boot_args.split():
            boot_args = f"{boot_args} {flag}".strip()
    apple_add["boot-args"] = boot_args

    nvram_delete = nvram.setdefault("Delete", {})
    apple_delete = list(nvram_delete.get(APPLE_NVRAM_GUID, []))
    if "csr-active-config" not in apple_delete:
        apple_delete.append("csr-active-config")
    nvram_delete[APPLE_NVRAM_GUID] = apple_delete

    oclp_delete = list(nvram_delete.get(OCLP_NVRAM_GUID, []))
    if "OCLP-Settings" not in oclp_delete:
        oclp_delete.append("OCLP-Settings")
    nvram_delete[OCLP_NVRAM_GUID] = oclp_delete

    oclp_settings = str(oclp_add.get("OCLP-Settings", ""))
    if "-allow_fv" not in oclp_settings.split():
        oclp_add["OCLP-Settings"] = f"{oclp_settings} -allow_fv".strip()

    security = config.setdefault("Misc", {}).setdefault("Security", {})
    security["SecureBootModel"] = "Disabled"

    for patch in config.get("Kernel", {}).get("Patch", []):
        if isinstance(patch, dict) and patch.get("Comment") == "Force FileVault on Broken Seal":
            patch["Enabled"] = True

    with plist_file.open("wb") as handle:
        plistlib.dump(config, handle, sort_keys=True)

    return inspect_efi_config(config)


def _diskutil_info(identifier: str) -> Optional[dict]:
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", identifier],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, TypeError):
        return None


def _ensure_partition_mounted(partition: str) -> tuple[Path, bool]:
    info = _diskutil_info(partition)
    if info and info.get("MountPoint"):
        mount_point = Path(info["MountPoint"])
        if mount_point.exists():
            return mount_point, False

    mount_res = subprocess_wrapper.run_as_root(
        ["/usr/sbin/diskutil", "mount", partition],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if mount_res.returncode != 0:
        subprocess_wrapper.log(mount_res)
        raise RuntimeError(f"Failed to mount EFI partition {partition}")

    refreshed = _diskutil_info(partition)
    mount_str = refreshed.get("MountPoint") if refreshed else None
    if not mount_str or not Path(mount_str).exists():
        raise RuntimeError(f"Mounted EFI partition {partition} has no accessible mount point")
    return Path(mount_str), True


def _unmount_if_needed(partition: str, did_mount: bool) -> None:
    if not did_mount:
        return
    subprocess.run(
        ["/usr/sbin/diskutil", "unmount", partition],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def enforce_esp_partition_root_patch_flags(esp_partition: str) -> Optional[dict]:
    """
    Mount an EFI System Partition if needed, enforce root-patch flags on
    EFI/OC/config.plist, and unmount if mounted by us.
    """
    mount_point, mounted_by_us = _ensure_partition_mounted(esp_partition)
    try:
        config_path = mount_point / "EFI/OC/config.plist"
        if not config_path.is_file():
            return None
        return enforce_efi_config_flags(config_path)
    finally:
        _unmount_if_needed(esp_partition, mounted_by_us)


class ESPSyncManager:
    """
    Inspect and synchronize OpenCore EFI on internal EFI System Partitions.
    """

    def __init__(self, global_constants: constants.Constants) -> None:
        self.constants: constants.Constants = global_constants

    def _whole_disk_for_store(self, store_identifier: str) -> str:
        info = _diskutil_info(store_identifier)
        if info and info.get("ParentWholeDisk"):
            return info["ParentWholeDisk"]
        return re.sub(r"s\d+$", "", store_identifier)

    def discover_esp_targets(
        self,
        target_group: Optional[mount.TargetVolumeGroup] = None,
    ) -> list[dict]:
        """
        Discover available GPT/FAT32 EFI partitions, prioritizing internal disks
        and the physical store backing target_group.
        """
        installer = install.disk_installation(self.constants)
        supported_disks = installer.list_disks()

        target_whole_disks = {
            self._whole_disk_for_store(store)
            for store in (target_group.physical_stores if target_group else [])
        }
        booted_oc_disk = getattr(self.constants, "booted_oc_disk", None)

        candidates: list[dict] = []
        for disk_id, disk_data in supported_disks.items():
            disk_info = _diskutil_info(disk_id) or {}
            bus_protocol = str(disk_info.get("BusProtocol", ""))
            is_internal = bool(disk_info.get("Internal", False)) and bus_protocol != "USB"
            is_target_disk = disk_id in target_whole_disks
            partitions = installer.list_partitions(disk_id, supported_disks)
            for part_id, part_data in partitions.items():
                is_booted_oc = bool(booted_oc_disk and part_id == booted_oc_disk)
                candidates.append(
                    {
                        "partition": part_id,
                        "disk": disk_id,
                        "disk_name": disk_data.get("name", disk_id),
                        "partition_name": part_data.get("name", "EFI"),
                        "size": part_data.get("size", ""),
                        "is_target_disk": is_target_disk,
                        "is_internal": is_internal,
                        "is_booted_oc": is_booted_oc,
                    }
                )

        def _sort_key(item: dict) -> tuple:
            return (
                0 if item["is_target_disk"] else 1,
                0 if item["is_internal"] else 1,
                1 if item["is_booted_oc"] else 0,
                item["partition"],
            )

        candidates.sort(key=_sort_key)
        return candidates

    def _prepare_build_constants(
        self,
        model: str,
        build_root: Path,
        host_is_target: bool,
    ) -> constants.Constants:
        build_constants = copy.copy(self.constants)
        build_constants.current_path = build_root
        build_constants.custom_model = model if not host_is_target else None
        if getattr(build_constants, "computer", None) is None:
            build_constants.computer = device_probe.Computer.probe()

        defaults.GenerateDefaults(
            model,
            host_is_target=host_is_target,
            global_constants=build_constants,
            ignore_settings_file=True,
        )
        enforce_constants_root_patch_flags(build_constants)
        return build_constants

    def sync_internal_esp(
        self,
        esp_partition: Optional[str] = None,
        target_group: Optional[mount.TargetVolumeGroup] = None,
        model: Optional[str] = None,
        mode: str = "rebuild",
        source_efi_partition: Optional[str] = None,
    ) -> dict:
        """
        Synchronize OpenCore onto an internal EFI System Partition.

        Args:
            esp_partition: Destination partition identifier (e.g. 'disk0s1').
                Auto-selected from discovered internal/target ESPs when omitted.
            target_group: Optional TargetVolumeGroup to prioritize its backing store ESP.
            model: Optional hardware SMBIOS override (e.g. 'MacPro6,1').
            mode: 'rebuild' (default, hardware-probed fresh OpenCore build with SIP/SBM enforced)
                or 'clone' (copies booted USB EFI and enforces root-patch flags).
            source_efi_partition: Source EFI partition when mode == 'clone'.
        """
        if mode not in ("rebuild", "clone"):
            raise ValueError(f"Unsupported ESP sync mode: {mode}")

        candidates = self.discover_esp_targets(target_group=target_group)
        chosen_esp = esp_partition
        if not chosen_esp:
            preferred = [
                c for c in candidates
                if (c["is_target_disk"] or c["is_internal"]) and not c["is_booted_oc"]
            ]
            if not preferred:
                preferred = [c for c in candidates if not c["is_booted_oc"]]
            if not preferred:
                raise RuntimeError("No internal or non-booted EFI System Partition found for ESP sync.")
            chosen_esp = preferred[0]["partition"]

        if mode == "clone":
            return self._clone_usb_efi(
                target_esp=chosen_esp,
                source_esp=source_efi_partition or getattr(self.constants, "booted_oc_disk", None),
            )

        if getattr(self.constants, "computer", None) is None:
            self.constants.computer = device_probe.Computer.probe()

        host_model = self.constants.computer.real_model if self.constants.computer else None
        selected_model = model or self.constants.custom_model or host_model
        if not selected_model:
            raise RuntimeError("Unable to determine Mac model for OpenCore build.")
        if selected_model not in model_array.SupportedSMBIOS and not self.constants.allow_oc_everywhere:
            raise ValueError(f"Unsupported SMBIOS model for OpenCore build: {selected_model}")

        host_is_target = bool(model is None and self.constants.custom_model is None)

        with tempfile.TemporaryDirectory(prefix="ocpbp_esp_build_") as temp_dir:
            build_constants = self._prepare_build_constants(
                model=selected_model,
                build_root=Path(temp_dir),
                host_is_target=host_is_target,
            )
            logging.info(f"Rebuilding OpenCore for {selected_model} -> {chosen_esp}")
            build.BuildOpenCore(selected_model, build_constants)
            inspection = enforce_efi_config_flags(build_constants.plist_path)
            installed = install.disk_installation(build_constants).install_opencore(chosen_esp)
            if not installed:
                raise RuntimeError(f"Failed to install rebuilt OpenCore onto {chosen_esp}")

        return {
            "mode": "rebuild",
            "esp_partition": chosen_esp,
            "model": selected_model,
            "inspection": inspection,
        }

    def _clone_usb_efi(self, target_esp: str, source_esp: Optional[str]) -> dict:
        if not source_esp:
            raise RuntimeError("No booted or specified source USB EFI partition available to clone.")
        if source_esp == target_esp:
            raise RuntimeError(f"Source and target EFI partitions are identical ({target_esp}).")

        src_mount, src_mounted = _ensure_partition_mounted(source_esp)
        try:
            src_oc_config = src_mount / "EFI/OC/config.plist"
            if not src_oc_config.is_file():
                raise FileNotFoundError(f"Source EFI partition {source_esp} does not contain EFI/OC/config.plist")

            dst_mount, dst_mounted = _ensure_partition_mounted(target_esp)
            try:
                logging.info(f"Cloning USB EFI from {source_esp} ({src_mount}) -> {target_esp} ({dst_mount})")
                for rel_remove in ("EFI/OC", "System", "boot.efi"):
                    target_item = dst_mount / rel_remove
                    if target_item.is_dir():
                        shutil.rmtree(target_item)
                    elif target_item.exists():
                        target_item.unlink()

                (dst_mount / "EFI").mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_mount / "EFI/OC", dst_mount / "EFI/OC", dirs_exist_ok=True)
                if (src_mount / "EFI/BOOT").is_dir():
                    shutil.copytree(src_mount / "EFI/BOOT", dst_mount / "EFI/BOOT", dirs_exist_ok=True)
                if (src_mount / "System").is_dir():
                    shutil.copytree(src_mount / "System", dst_mount / "System", dirs_exist_ok=True)
                if (src_mount / "boot.efi").is_file():
                    shutil.copy2(src_mount / "boot.efi", dst_mount / "boot.efi")
                if (src_mount / ".VolumeIcon.icns").is_file():
                    shutil.copy2(src_mount / ".VolumeIcon.icns", dst_mount / ".VolumeIcon.icns")

                inspection = enforce_efi_config_flags(dst_mount / "EFI/OC/config.plist")
            finally:
                if not getattr(self.constants, "recovery_status", False):
                    _unmount_if_needed(target_esp, dst_mounted)
        finally:
            _unmount_if_needed(source_esp, src_mounted)

        return {
            "mode": "clone",
            "esp_partition": target_esp,
            "source_esp": source_esp,
            "inspection": inspection,
        }

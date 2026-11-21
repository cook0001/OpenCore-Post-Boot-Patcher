"""
ocpbp: OpenCore Post-Boot Patcher offline recovery CLI/TUI and ESP synchronization helpers.
"""

from .esp_sync import (
    REQUIRED_ROOT_PATCH_CSR_MASK,
    ROOT_PATCH_CSR_BYTES,
    ESPSyncManager,
    discover_offline_assets_dir,
    enforce_constants_root_patch_flags,
    enforce_efi_config_flags,
    enforce_esp_partition_root_patch_flags,
    inspect_efi_config,
    summarize_offline_assets,
)
from .tui import OCPBPTerminalUI

__all__ = [
    "REQUIRED_ROOT_PATCH_CSR_MASK",
    "ROOT_PATCH_CSR_BYTES",
    "ESPSyncManager",
    "OCPBPTerminalUI",
    "discover_offline_assets_dir",
    "enforce_constants_root_patch_flags",
    "enforce_efi_config_flags",
    "enforce_esp_partition_root_patch_flags",
    "inspect_efi_config",
    "summarize_offline_assets",
]

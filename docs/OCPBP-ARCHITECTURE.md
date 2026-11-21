# OpenCore Post-Boot Patcher (`OCPBP`) — Architecture & Plan

Base: OCLP `macpro61-tahoe` (`5c496d37`). Documentation site: [ocpbp.patchcraft.dev](https://ocpbp.patchcraft.dev/).

## Goal

Eliminate post-install SSH workarounds on unsupported Macs where fresh macOS boots without usable graphics acceleration, `WindowServer`, or legacy USB input. After installing macOS to internal storage, boot once from the **OCPBP USB** (via OpenCore), run the offline post-boot patcher against the target internal macOS volume, create the APFS snapshot, and reboot directly into a patched macOS desktop.

## Core Design: Version-Matched `BaseSystem` + Companion `OCPBP` Partition

Rather than shipping a duplicate 25–35 GB macOS installation on USB, `OCPBP` reuses Apple's own macOS Installer / Recovery environment alongside an offline payload volume:

* **1. USB OpenCore ESP (`EFI`)**: Model-specific OpenCore configuration enforcing SIP / Authenticated-Root flags (`csr-active-config`) and `SecureBootModel=Disabled` independently of internal NVRAM.
* **2. macOS Installer / Recovery (`BaseSystem`)**: Boots with basic GOP/VESA graphics and Apple USB drivers while providing version-matched `/usr/bin/kmutil`, `/usr/sbin/bless`, `/usr/sbin/diskutil`, `/sbin/mount_apfs`, and `apfs` utilities.
* **3. Companion `OCPBP` Payload Volume (APFS or JHFS+)**: Preserves POSIX permissions and symlinks offline and bundles:
  * Self-contained relocatable OCLP CLI / TUI runner (zero `wxPython` / `AppKit` / `AppleScript` runtime dependency)
  * `Universal-Binaries.dmg` (`PatcherSupportPkg`)
  * Pre-downloaded matching **KDK** and **MetallibSupportPkg** so root patching requires zero Wi-Fi/Ethernet in Recovery
  * Entry launcher (`/Volumes/OCPBP/ocpbp.command`)

## Implemented Root-Cause Architecture (Phases 1–5)

* **Decouple Host Hardware Probe from Target OS Detection**: Parameterized `OSProbe` (`detections/os_probe.py`) and Tahoe XNU/Marketing version mapping (`datasets/os_data.py`) inspect `<target_root>/System/Library/CoreServices/SystemVersion.plist` and target Preboot Cryptex metadata while probing live hardware via `IORegistry`.
* **Offline Target Support in `RootVolumeMount` & `APFSSnapshot`**: `sys_patch/mount/mount.py` and `snapshot.py` discover/resolve APFS volume groups (`TargetVolumeGroup`: `System` + paired `Data` + `Preboot`), unlock FileVault (`unlock_data_volume`), and manage `/tmp/ocpbp_*` staging mounts cleanly.
* **Target-Scoped `PatchSysVolume`, `KernelDebugKitMerge`, & Cache Builders**: `sys_patch/` routes auxiliary/boot/system kernel collections, KDK & Metallib staging from `/Volumes/OCPBP`, `LaunchAgents`/`LaunchDaemons`, `RSRRepair`, and offline `AutoPkg-Assets.pkg` app/helper installation strictly against the selected target volume group.
* **Headless / Recovery-Safe CLI, TUI & Internal ESP Sync**: `ocpbp_cli.py`, `ocpbp/tui.py`, and `ocpbp/esp_sync.py` provide zero-GUI Recovery operation (`--ocpbp`, `--target_vol`, `--unpatch_target_vol`, `--offline_assets_dir`, `--sync_esp`, `--passphrase`), auto-discover `/Volumes/OCPBP`, validate SIP/AR boot bits, and rebuild or clone root-patch-ready OpenCore onto the internal ESP.
* **Installer USB Builder & Standalone Packaging**: `support/macos_installer_handler.py`, `wx_gui/gui_macos_installer_flash.py`, `OpenCore-Post-Boot-Patcher.spec`, and `Build-Project.command` pre-partition 32 GB+ USBs into `EFI` + dynamically sized `OCLP-Installer` + `OCPBP` (with automatic fallback for smaller drives), enforce root-patch USB EFI flags, eliminate `/Library/Packages` duplication, and build `dist/ocpbp` cleanly in CI/Release workflows.

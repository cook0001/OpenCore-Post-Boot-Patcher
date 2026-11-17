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

## Planned Root-Cause Refactors

* **Decouple Host Hardware Probe from Target OS Detection**: Extend `OSProbe` to inspect `<target_root>/System/Library/CoreServices/SystemVersion.plist` (and target Preboot Cryptex metadata when present) while probing live hardware via `IORegistry`.
* **Offline Target Support in `RootVolumeMount` & `APFSSnapshot`**: Parameterize APFS volume group resolution (`System` + paired `Data` + `Preboot`) and safe staging mount points instead of hardcoding `/` and `/System/Volumes/Update/mnt1`.
* **Target-Scoped `PatchSysVolume`, `KernelDebugKitMerge`, & Cache Builders**: Route `/Library/Application Support` / `/Library/Extensions` writes, KDK/Metallib staging, and `kmutil` cache rebuilds (`BootKC` / `SysKC` / `AuxKC`) strictly against the selected target volume.
* **Headless / Recovery-Safe CLI & TUI Runtime**: Lazy-gate `wx_gui` and `applescript` imports behind runtime checks, add `--target_vol`, `--unpatch_target_vol`, `--offline_assets_dir`, and `--ocpbp`, validate SIP/AR boot bits before patching, and offer one-step internal ESP OpenCore sync.
* **Installer USB Builder Option (`Build OCPBP USB`)**: Extend `macos_installer_handler.py` and GUI installer flows to optionally add and populate the companion `OCPBP` payload volume on installer media.

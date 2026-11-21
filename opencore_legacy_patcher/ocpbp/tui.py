"""
tui.py: Pure-stdlib interactive Recovery Terminal UI for OpenCore Post-Boot Patcher.
Zero dependency on wxPython, AppKit, or AppleScript.
"""

import getpass
import logging
import plistlib
import subprocess

from pathlib import Path
from typing import Callable, Optional, Union

from .. import constants
from ..datasets import model_array
from ..support import subprocess_wrapper, utilities
from ..sys_patch import sys_patch
from ..sys_patch.mount import mount
from . import esp_sync


class OCPBPTerminalUI:
    """
    Numbered-menu Terminal UI for offline Recovery root patching and ESP sync.
    """

    def __init__(
        self,
        global_constants: constants.Constants,
        offline_assets_dir: Optional[Union[str, Path]] = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        passphrase_fn: Callable[[str], str] = getpass.getpass,
    ) -> None:
        self.constants: constants.Constants = global_constants
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.passphrase_fn = passphrase_fn

        discovered = esp_sync.discover_offline_assets_dir(
            explicit_dir=offline_assets_dir,
            global_constants=self.constants,
        )
        if discovered:
            self.constants.offline_assets_dir = discovered

        self.resolver = mount.APFSVolumeResolver()
        self.esp_manager = esp_sync.ESPSyncManager(self.constants)
        self.discovered_targets: list[mount.TargetVolumeGroup] = []
        self.selected_target: Optional[mount.TargetVolumeGroup] = None
        self.target_passphrase: Optional[str] = None

        self.refresh_targets(auto_select_single=True)

    def _say(self, message: str = "") -> None:
        self.output_fn(message)

    def _ask(self, prompt: str) -> str:
        try:
            return self.input_fn(prompt).strip()
        except EOFError:
            return "0"

    def refresh_targets(self, auto_select_single: bool = False) -> list[mount.TargetVolumeGroup]:
        try:
            self.discovered_targets = self.resolver.discover_volume_groups(include_live_root=False)
        except Exception as exc:
            logging.warning(f"Failed to discover APFS targets: {exc}")
            self.discovered_targets = []

        if self.selected_target:
            matched = next(
                (
                    g for g in self.discovered_targets
                    if g.system_identifier == self.selected_target.system_identifier
                ),
                None,
            )
            self.selected_target = matched

        if auto_select_single and self.selected_target is None and len(self.discovered_targets) == 1:
            self.selected_target = self.discovered_targets[0]

        return self.discovered_targets

    def _format_target_label(self, group: mount.TargetVolumeGroup) -> str:
        mount_str = group.system_mount or f"/dev/{group.system_identifier}"
        flags = []
        if group.filevault_enabled:
            flags.append("FileVault: Locked" if group.data_locked else "FileVault: Unlocked")
        if group.data_identifier:
            flags.append(f"Data={group.data_identifier}")
        extra = f" ({', '.join(flags)})" if flags else ""
        return f"{group.volume_name} [{group.system_identifier}] @ {mount_str}{extra}"

    def _render_banner(self) -> None:
        model = self.constants.custom_model or (
            self.constants.computer.real_model if self.constants.computer else "Unknown"
        )
        env_label = "RecoveryOS" if self.constants.recovery_status else "macOS"
        sec = esp_sync.get_live_boot_security_status()
        assets_dir = self.constants.offline_assets_dir
        summary = esp_sync.summarize_offline_assets(assets_dir)
        present_assets = [key for key, path in summary.items() if path is not None and key != "manifest"]
        if sec["secure_boot_active"] is None:
            sbm_label = "Unknown"
        else:
            sbm_label = "Active" if sec["secure_boot_active"] else "Disabled"

        self._say("")
        self._say(f"OpenCore Post-Boot Patcher (OCPBP) v{self.constants.ocpbp_version} (OCLP {self.constants.patcher_version})")
        self._say(f"  Target Model : {model}")
        self._say(
            f"  Boot Context : {env_label} | SIP={sec['csr_active_config_hex']} "
            f"({'Ready' if sec['sip_allows_root_patch'] else 'BLOCKED'}) | SecureBoot={sbm_label}"
        )
        if assets_dir:
            asset_text = ", ".join(present_assets) if present_assets else "None found"
            self._say(f"  Offline Dir  : {assets_dir} [{asset_text}]")
        else:
            self._say("  Offline Dir  : Not detected (set via option 5 or --offline_assets_dir)")

        if self.selected_target:
            self._say(f"  Target Volume: {self._format_target_label(self.selected_target)}")
        else:
            self._say(
                f"  Target Volume: None selected ({len(self.discovered_targets)} offline macOS volume(s) found)"
            )
        self._say("")
        self._say("  1) Select / Refresh Target macOS Volume")
        self._say("  2) Patch Target macOS Volume Offline")
        self._say("  3) Sync OpenCore to Internal ESP (Rebuild / Clone USB EFI)")
        self._say("  4) Unpatch Target macOS Volume (Revert Snapshot)")
        self._say("  5) Diagnostics, EFI Inspection & Offline Payload Settings")
        self._say("  6) Reboot Machine")
        self._say("  0) Exit")

    def _ensure_target_unlocked(self, group: mount.TargetVolumeGroup) -> mount.TargetVolumeGroup:
        if not group.data_locked:
            return group

        if self.target_passphrase is None:
            label = group.data_volume_name or group.data_identifier or group.volume_name
            self.target_passphrase = self.passphrase_fn(
                f"Enter FileVault password or recovery key for '{label}': "
            )
        unlocked = self.resolver.unlock_data_volume(
            group,
            passphrase=self.target_passphrase,
            interactive=False,
        )
        self.selected_target = unlocked
        return unlocked

    def action_select_target(self) -> Optional[mount.TargetVolumeGroup]:
        targets = self.refresh_targets(auto_select_single=False)
        self._say("")
        self._say("Discovered Offline macOS APFS Volume Groups:")
        if targets:
            for idx, group in enumerate(targets, start=1):
                marker = "*" if self.selected_target and self.selected_target.system_identifier == group.system_identifier else " "
                self._say(f"  {marker} {idx}) {self._format_target_label(group)}")
        else:
            self._say("  (No non-live macOS APFS volume groups auto-discovered)")
        self._say("    m) Enter custom path, disk identifier, or UUID manually")
        self._say("    r) Refresh list")
        self._say("    0) Back")

        choice = self._ask("Select target [0]: ")
        if not choice or choice == "0":
            return self.selected_target
        if choice.lower() == "r":
            return self.action_select_target()
        if choice.lower() == "m":
            raw_target = self._ask("Enter target mount path, disk ID (e.g. disk1s5), or UUID: ")
            if not raw_target:
                return self.selected_target
            try:
                resolved = self.resolver.resolve_target(raw_target)
            except Exception as exc:
                self._say(f"[ERROR] {exc}")
                return self.selected_target
            self.selected_target = resolved
        else:
            try:
                index = int(choice)
                if 1 <= index <= len(targets):
                    self.selected_target = targets[index - 1]
                else:
                    self._say("[ERROR] Selection out of range.")
                    return self.selected_target
            except ValueError:
                self._say("[ERROR] Invalid choice.")
                return self.selected_target

        self.target_passphrase = None
        if self.selected_target and self.selected_target.data_locked:
            unlock_ans = self._ask("Target Data volume is FileVault-locked. Unlock now? [Y/n]: ")
            if unlock_ans.lower() in ("", "y", "yes"):
                try:
                    self._ensure_target_unlocked(self.selected_target)
                    self._say("[OK] Target Data volume unlocked.")
                except Exception as exc:
                    self.target_passphrase = None
                    self._say(f"[ERROR] Unlock failed: {exc}")

        return self.selected_target

    def _require_target(self) -> Optional[mount.TargetVolumeGroup]:
        if self.selected_target is not None:
            return self.selected_target
        self.refresh_targets(auto_select_single=True)
        if self.selected_target is not None:
            return self.selected_target
        return self.action_select_target()

    def _run_target_operation(self, unpatch: bool = False) -> bool:
        target = self._require_target()
        if target is None:
            self._say("[ERROR] No target volume selected.")
            return False

        if unpatch:
            confirm = self._ask(
                f"Revert APFS root patches on '{target.volume_name}' ({target.system_identifier})? [y/N]: "
            )
            if confirm.lower() not in ("y", "yes"):
                return False
        else:
            if target.is_live_booted:
                confirm_live = self._ask(
                    "WARNING: Selected target is the currently booted root filesystem (/). Continue? [y/N]: "
                )
                if confirm_live.lower() not in ("y", "yes"):
                    return False

            sec = esp_sync.get_live_boot_security_status()
            if not sec["ready_for_root_patch"]:
                self._say(
                    f"[WARNING] Current boot security state does not permit root patching "
                    f"(SIP={sec['csr_active_config_hex']}, SecureBootActive={sec['secure_boot_active']})."
                )
                self._say("Ensure you booted from the OCPBP USB OpenCore bootloader or confirm below.")
                proceed = self._ask("Proceed anyway (preflight will enforce policy checks)? [y/N]: ")
                if proceed.lower() not in ("y", "yes"):
                    return False

        if target.data_locked:
            try:
                target = self._ensure_target_unlocked(target)
            except Exception as exc:
                self.target_passphrase = None
                self._say(f"[ERROR] Failed to unlock FileVault target: {exc}")
                return False

        target_ref = target.system_mount or f"/dev/{target.system_identifier}"
        model = self.constants.custom_model or (
            self.constants.computer.real_model if self.constants.computer else ""
        )
        op_name = "unpatch" if unpatch else "root patch"
        self._say(f"Starting offline {op_name} for '{target.volume_name}' ({target_ref})...")

        patcher = sys_patch.PatchSysVolume(
            model,
            self.constants,
            hardware_details=None,
            target_vol=target_ref,
            offline_assets_dir=self.constants.offline_assets_dir,
            passphrase=self.target_passphrase,
            interactive_unlock=False,
        )
        if unpatch:
            patcher.start_unpatch()
        else:
            patcher.start_patch()

        if self.constants.root_patcher_succeeded:
            if unpatch:
                self._say("[OK] Target volume unpatched and snapshot reverted.")
            else:
                self._say("[OK] Root patches applied and sealed APFS snapshot created.")
                sync_ans = self._ask("Sync OpenCore to internal ESP now (Recommended)? [Y/n]: ")
                if sync_ans.lower() in ("", "y", "yes"):
                    self.action_sync_esp()
            return True

        self._say(f"[ERROR] Offline {op_name} did not complete cleanly.")
        return False

    def action_patch_target(self) -> bool:
        return self._run_target_operation(unpatch=False)

    def action_unpatch_target(self) -> bool:
        return self._run_target_operation(unpatch=True)

    def action_sync_esp(self) -> bool:
        try:
            candidates = self.esp_manager.discover_esp_targets(target_group=self.selected_target)
        except Exception as exc:
            self._say(f"[ERROR] Failed to discover EFI partitions: {exc}")
            return False

        if not candidates:
            self._say("[ERROR] No GPT/FAT32 EFI partitions found.")
            return False

        self._say("")
        self._say("Available EFI System Partitions:")
        for idx, cand in enumerate(candidates, start=1):
            tags = []
            if cand["is_target_disk"]:
                tags.append("TARGET DISK")
            elif cand["is_internal"]:
                tags.append("INTERNAL")
            else:
                tags.append("EXTERNAL")
            if cand["is_booted_oc"]:
                tags.append("BOOTED OC")
            self._say(
                f"  {idx}) {cand['partition']} - {cand['disk_name']} ({cand['size']}) [{' / '.join(tags)}]"
            )

        pick = self._ask("Select destination ESP number [1] (or 0 to cancel): ")
        if pick == "0":
            return False
        if not pick:
            index = 1
        else:
            try:
                index = int(pick)
            except ValueError:
                self._say("[ERROR] Invalid selection.")
                return False

        if not (1 <= index <= len(candidates)):
            self._say("[ERROR] Selection out of range.")
            return False

        chosen = candidates[index - 1]["partition"]

        self._say("ESP Sync Mode:")
        self._say("  1) Rebuild hardware-probed OpenCore with root-patch SIP + SecureBootModel=Disabled [Recommended]")
        self._say("  2) Clone currently booted USB EFI and enforce root-patch flags")
        mode_choice = self._ask("Select mode [1]: ")
        mode = "clone" if mode_choice == "2" else "rebuild"

        try:
            result = self.esp_manager.sync_internal_esp(
                esp_partition=chosen,
                target_group=self.selected_target,
                model=self.constants.custom_model,
                mode=mode,
            )
        except Exception as exc:
            self._say(f"[ERROR] ESP sync failed: {exc}")
            return False

        insp = result.get("inspection", {})
        self._say(
            f"[OK] Synced OpenCore ({result['mode']}) to {result['esp_partition']} | "
            f"SIP={insp.get('csr_active_config_hex')} | "
            f"SecureBootModel={insp.get('secure_boot_model')} | "
            f"-allow_fv={insp.get('allow_fv_enabled')}"
        )
        return True

    def action_diagnostics(self) -> None:
        self._say("")
        self._say("--- OCPBP Diagnostics & Settings ---")
        sec = esp_sync.get_live_boot_security_status()
        self._say(f"Live NVRAM Security: {sec}")
        self._say(f"Offline Assets Dir : {self.constants.offline_assets_dir}")

        summary = esp_sync.summarize_offline_assets(self.constants.offline_assets_dir)
        for key, path in summary.items():
            self._say(f"  - {key}: {path if path else 'Missing'}")

        manifest_path = summary.get("manifest")
        if manifest_path and manifest_path.is_file():
            try:
                with manifest_path.open("rb") as handle:
                    manifest = plistlib.load(handle)
                self._say(f"OCPBP Manifest     : {manifest}")
            except Exception as exc:
                self._say(f"OCPBP Manifest     : Unable to read ({exc})")

        try:
            candidates = self.esp_manager.discover_esp_targets(target_group=self.selected_target)
            for cand in candidates:
                self._say(
                    f"ESP Candidate      : {cand['partition']} ({cand['disk_name']}, "
                    f"internal={cand['is_internal']}, target={cand['is_target_disk']}, booted={cand['is_booted_oc']})"
                )
        except Exception as exc:
            self._say(f"ESP Discovery Error: {exc}")

        self._say("")
        self._say("Options:")
        self._say("  1) Set custom Offline Assets Directory")
        self._say("  2) Set custom Target Model override")
        self._say("  0) Back")
        opt = self._ask("Choose option [0]: ")
        if opt == "1":
            new_dir = self._ask("Enter path to offline assets directory: ")
            if new_dir:
                try:
                    resolved_dir = esp_sync.discover_offline_assets_dir(
                        explicit_dir=new_dir,
                        global_constants=self.constants,
                    )
                    self.constants.offline_assets_dir = resolved_dir
                    self._say(f"[OK] Offline assets directory set to {resolved_dir}")
                except Exception as exc:
                    self._say(f"[ERROR] {exc}")
        elif opt == "2":
            new_model = self._ask("Enter Mac model (e.g. MacPro6,1) or 'clear' to use host model: ")
            if new_model.lower() == "clear":
                self.constants.custom_model = None
                self._say("[OK] Cleared custom model override.")
            elif new_model:
                if new_model in model_array.SupportedSMBIOS or self.constants.allow_oc_everywhere:
                    self.constants.custom_model = new_model
                    self._say(f"[OK] Custom Target Model set to {new_model}")
                else:
                    self._say(f"[ERROR] Unrecognized SMBIOS model: {new_model}")

    def action_reboot(self) -> bool:
        confirm = self._ask("Reboot the machine now? [y/N]: ")
        if confirm.lower() not in ("y", "yes"):
            return False
        self._say("Rebooting...")
        subprocess_wrapper.run_as_root(["/sbin/reboot"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True

    def run(self, max_iterations: Optional[int] = None) -> int:
        iterations = 0
        while True:
            if max_iterations is not None and iterations >= max_iterations:
                return 0
            iterations += 1

            self._render_banner()
            choice = self._ask("Enter choice [0-6]: ")
            if choice == "0" or choice.lower() in ("q", "quit", "exit"):
                self._say("Exiting OCPBP.")
                return 0
            if choice == "1":
                self.action_select_target()
            elif choice == "2":
                self.action_patch_target()
            elif choice == "3":
                self.action_sync_esp()
            elif choice == "4":
                self.action_unpatch_target()
            elif choice == "5":
                self.action_diagnostics()
            elif choice == "6":
                if self.action_reboot():
                    return 0
            else:
                self._say(f"[ERROR] Unknown menu choice: {choice}")

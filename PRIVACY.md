# Privacy Policy

**Repository:** [https://github.com/cook0001/OpenCore-Post-Boot-Patcher](https://github.com/cook0001/OpenCore-Post-Boot-Patcher)  
**Project Site:** [https://ocpbp.patchcraft.dev/](https://ocpbp.patchcraft.dev/)  
**Legal & Attributions:** [https://patchcraft.dev/legal.html](https://patchcraft.dev/legal.html)

## Telemetry & Crash Reporting

Builds produced from this repository (`cook0001/OpenCore-Post-Boot-Patcher`) compile with empty `ANALYTICS_SERVER` and `SITE_KEY` values in `opencore_legacy_patcher/support/analytics_handler.py`. **No background telemetry, hardware UUIDs, or crash logs are transmitted** to PatchCraft or to Dortania.

## Offline Recovery & User-Initiated Network Requests

- **macOS Recovery (`ocpbp` CLI/TUI):** Designed for 100% offline USB root patching and makes zero network requests during Recovery execution.
- **Desktop USB Staging (`OpenCore-Patcher.app`):** Network requests occur only when you explicitly download macOS installers, KDKs, Metallibs, or updates from Apple Software Update servers, GitHub Releases, or upstream Dortania manifests.

## Upstream Attribution & Non-Affiliation

> This product includes software developed by Dortania and OpenCore Legacy Patcher contributors.

This repository is an independent fork derived from [dortania/OpenCore-Legacy-Patcher](https://github.com/dortania/OpenCore-Legacy-Patcher) under the 4-clause BSD license in [LICENSE.txt](LICENSE.txt). It is not affiliated with or endorsed by Dortania or Apple Inc.

## Contact

For privacy or security questions regarding this repository, email [support@patchcraft.dev](mailto:support@patchcraft.dev) or open an issue / security advisory at [https://github.com/cook0001/OpenCore-Post-Boot-Patcher/issues](https://github.com/cook0001/OpenCore-Post-Boot-Patcher/issues).

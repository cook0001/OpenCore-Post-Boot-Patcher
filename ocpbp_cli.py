#!/usr/bin/env python3
"""
ocpbp_cli.py: Headless Recovery & CLI Entry Point for OpenCore Post-Boot Patcher
"""

from opencore_legacy_patcher import application_entry


if __name__ == "__main__":
    application_entry.main(headless=True)

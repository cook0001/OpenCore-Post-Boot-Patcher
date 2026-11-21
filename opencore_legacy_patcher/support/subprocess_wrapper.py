"""
subprocess_wrapper.py: Wrapper for subprocess module to better handle errors and output
                       Additionally handles our Privileged Helper Tool
"""

import enum
import logging
import os
import shlex
import subprocess
import sys

from pathlib import Path


OCLP_PRIVILEGED_HELPER = "/Library/PrivilegedHelperTools/com.dortania.opencore-legacy-patcher.privileged-helper"
_OSASCRIPT_PATH = "/usr/bin/osascript"
_SUDO_PATH = "/usr/bin/sudo"


class PrivilegedHelperErrorCodes(enum.IntEnum):
    """
    Error codes for Privileged Helper Tool.

    Reference:
        payloads/Tools/PrivilegedHelperTool/main.m
    """
    OCLP_PHT_ERROR_MISSING_ARGUMENTS           = 160
    OCLP_PHT_ERROR_SET_UID_MISSING             = 161
    OCLP_PHT_ERROR_SET_UID_FAILED              = 162
    OCLP_PHT_ERROR_SELF_PATH_MISSING           = 163
    OCLP_PHT_ERROR_PARENT_PATH_MISSING         = 164
    OCLP_PHT_ERROR_SIGNING_INFORMATION_MISSING = 165
    OCLP_PHT_ERROR_INVALID_TEAM_ID             = 166
    OCLP_PHT_ERROR_INVALID_CERTIFICATES        = 167
    OCLP_PHT_ERROR_COMMAND_MISSING             = 168
    OCLP_PHT_ERROR_COMMAND_FAILED              = 169
    OCLP_PHT_ERROR_CATCH_ALL                   = 170


_HELPER_AUTH_ERROR_CODES = {
    PrivilegedHelperErrorCodes.OCLP_PHT_ERROR_SELF_PATH_MISSING.value,
    PrivilegedHelperErrorCodes.OCLP_PHT_ERROR_PARENT_PATH_MISSING.value,
    PrivilegedHelperErrorCodes.OCLP_PHT_ERROR_SIGNING_INFORMATION_MISSING.value,
    PrivilegedHelperErrorCodes.OCLP_PHT_ERROR_INVALID_TEAM_ID.value,
    PrivilegedHelperErrorCodes.OCLP_PHT_ERROR_INVALID_CERTIFICATES.value,
}


def _applescript_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _is_interactive_tty() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:
        return False


def _should_use_osascript() -> bool:
    if not Path(_OSASCRIPT_PATH).exists():
        return False
    if os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"):
        return False
    if _is_interactive_tty() and len(sys.argv) > 1:
        return False
    return True


def _run_via_osascript(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    shell_cmd = " ".join(shlex.quote(arg) for arg in command)
    script = f"do shell script {_applescript_quote(shell_cmd)} with administrator privileges"
    return subprocess.run([_OSASCRIPT_PATH, "-e", script], **kwargs)


def _run_via_sudo(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([_SUDO_PATH] + command, **kwargs)


def run(*args, **kwargs) -> subprocess.CompletedProcess:
    """
    Basic subprocess.run wrapper.
    """
    return subprocess.run(*args, **kwargs)


def run_as_root(*args, **kwargs) -> subprocess.CompletedProcess:
    """
    Run subprocess as root.

    Note: Full path to first argument is required.
    Helper tool does not resolve PATH.
    """
    command = [str(arg) for arg in args[0]]

    # Check if first argument exists
    if not Path(command[0]).exists():
        raise FileNotFoundError(f"File not found: {command[0]}")

    if os.geteuid() == 0:
        return subprocess.run(command, **kwargs)

    helper_result = None
    if Path(OCLP_PRIVILEGED_HELPER).exists():
        helper_result = subprocess.run([OCLP_PRIVILEGED_HELPER] + command, **kwargs)
        if helper_result.returncode not in _HELPER_AUTH_ERROR_CODES:
            return helper_result
        logging.warning(
            f"Privileged helper rejected elevation (exit {helper_result.returncode}); falling back to system authorization"
        )

    if _should_use_osascript():
        osascript_result = _run_via_osascript(command, **kwargs)
        if osascript_result.returncode == 0:
            return osascript_result
        err_text = ""
        for stream in (osascript_result.stderr, osascript_result.stdout):
            if isinstance(stream, bytes):
                err_text += stream.decode("utf-8", errors="replace")
            elif isinstance(stream, str):
                err_text += stream
        if ("No user interaction allowed" in err_text or "-1713" in err_text) and _is_interactive_tty() and Path(_SUDO_PATH).exists():
            return _run_via_sudo(command, **kwargs)
        return osascript_result

    if Path(_SUDO_PATH).exists():
        return _run_via_sudo(command, **kwargs)

    if helper_result is not None:
        return helper_result

    raise PermissionError("Root privileges required and no elevation method is available")


def verify(process_result: subprocess.CompletedProcess) -> None:
    """
    Verify process result and raise exception if failed.
    """
    if process_result.returncode == 0:
        return

    log(process_result)

    raise Exception(f"Process failed with exit code {process_result.returncode}")


def run_and_verify(*args, **kwargs) -> None:
    """
    Run subprocess and verify result.

    Asserts on failure.
    """
    verify(run(*args, **kwargs))


def run_as_root_and_verify(*args, **kwargs) -> None:
    """
    Run subprocess as root and verify result.

    Asserts on failure.
    """
    verify(run_as_root(*args, **kwargs))


def log(process: subprocess.CompletedProcess) -> None:
    """
    Display subprocess error output in formatted string.
    """
    for line in generate_log(process).split("\n"):
        logging.error(line)


def generate_log(process: subprocess.CompletedProcess) -> str:
    """
    Display subprocess error output in formatted string.
    Note this function is still used for zero return code errors, since
    some software don't ever return non-zero regardless of success.

    Format:

        Command: <command>
        Return Code: <return code>
        Standard Output:
            <standard output line 1>
            <standard output line 2>
            ...
        Standard Error:
            <standard error line 1>
            <standard error line 2>
            ...
    """
    output = "Subprocess failed.\n"
    output += f"    Command: {process.args}\n"
    output += f"    Return Code: {process.returncode}\n"
    _returned_error = __resolve_privileged_helper_errors(process.returncode)
    if _returned_error:
        output += f"        Likely Enum: {_returned_error}\n"
    output += f"    Standard Output:\n"
    if process.stdout:
        stdout_str = process.stdout.decode("utf-8", errors="replace") if isinstance(process.stdout, bytes) else str(process.stdout)
        output += __format_output(stdout_str)
    else:
        output += "        None\n"
    output += f"    Standard Error:\n"
    if process.stderr:
        stderr_str = process.stderr.decode("utf-8", errors="replace") if isinstance(process.stderr, bytes) else str(process.stderr)
        output += __format_output(stderr_str)
    else:
        output += "        None\n"

    return output


def __resolve_privileged_helper_errors(return_code: int) -> str:
    """
    Attempt to resolve Privileged Helper Tool error codes.
    """
    if return_code not in [error_code.value for error_code in PrivilegedHelperErrorCodes]:
        return None

    return PrivilegedHelperErrorCodes(return_code).name


def __format_output(output: str) -> str:
    """
    Format output.
    """
    if not output:
        # Shouldn't happen, but just in case
        return "        None\n"

    _result = "\n".join([f"        {line}" for line in output.split("\n") if line not in ["", "\n"]])
    if not _result.endswith("\n"):
        _result += "\n"

    return _result
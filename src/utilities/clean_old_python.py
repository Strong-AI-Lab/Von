import os
import subprocess
import re
import ctypes
import sys
from typing import Any, cast


def _get_windows_shell32() -> Any:
    """Return shell32 on Windows; raise on unsupported platforms."""
    if os.name != "nt":
        raise OSError("Windows-only operation")
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise AttributeError("ctypes.windll is unavailable")
    return cast(Any, windll).shell32


def is_admin():
    """Check if the script is running with administrative privileges."""
    try:
        shell32 = _get_windows_shell32()
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_privileges():
    """Restart the script with elevated privileges if not already running as admin."""
    if not is_admin():
        shell32 = _get_windows_shell32()
        shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1
        )
        sys.exit(0)


def get_installed_python_versions():
    """Get all installed Python versions using the 'py' launcher."""
    versions = []
    try:
        result = subprocess.run(["py", "-0p"], capture_output=True, text=True)
        if result.returncode == 0:
            output = result.stdout
            version_pattern = re.compile(r"-V:(\d+\.\d+(\.\d+)?)")
            versions = version_pattern.findall(output)
            versions = [v[0] for v in versions]
    except Exception as e:
        print(f"Error listing Python versions: {e}")
    return versions


def normalize_version(version):
    """Normalize version strings into tuples for comparison."""
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    return tuple(map(int, parts))


def find_python_in_paths():
    """Find Python executables in PATH."""
    paths = os.getenv("PATH", "").split(os.pathsep)
    detected_versions = {}
    for path in paths:
        python_executable = os.path.join(path, "python.exe")
        if os.path.exists(python_executable):
            try:
                result = subprocess.run(
                    [python_executable, "--version"], capture_output=True, text=True
                )
                if result.returncode == 0:
                    version = result.stdout.strip().split()[-1]
                    detected_versions[version] = python_executable
            except Exception as e:
                print(f"Error detecting Python version in {path}: {e}")
    for version, location in detected_versions.items():
        print(f"Detected Python {version} at {location}")
    return detected_versions


def handle_microsoft_store_python(location):
    """Handle Python installed via the Microsoft Store."""
    print(f"The Python installation at {location} is a Microsoft Store installation.")
    print("To remove it, follow these steps:")
    print("1. Open the Start menu and search for 'Apps & Features'.")
    print("2. Locate 'Python' in the list of installed apps.")
    print("3. Select the entry and click 'Uninstall'.")
    print("4. Confirm the uninstallation.")
    print(
        "Alternatively, you can manually delete the placeholder file, but this is not recommended."
    )


def main():
    print("Cleaning up old Python versions... version GPT 5")
    elevate_privileges()

    versions = get_installed_python_versions()
    if not versions:
        print("No Python versions found.")
        return

    print("Installed Python versions:")
    for version in versions:
        print(version)

    versions_to_uninstall = [v for v in versions if normalize_version(v) < (3, 13, 0)]
    if not versions_to_uninstall:
        print("No Python versions earlier than 3.13 found.")
        return

    print("\nPython versions to uninstall:")
    for version in versions_to_uninstall:
        print(version)

    # Check PATH for Python installations
    paths = find_python_in_paths()

    for version in versions_to_uninstall:
        if version in paths:
            location = paths[version]
            if "WindowsApps" in location:
                handle_microsoft_store_python(location)
            else:
                print(
                    f"Python {version} detected at {location}, but no automated uninstallation is available."
                )
        else:
            print(
                f"Python {version} appears to be installed but could not be uninstalled programmatically."
            )


if __name__ == "__main__":
    main()

import os
import subprocess
def open_folder(path):
    if os.name == 'nt':  # For Windows
        os.startfile(path)
    elif os.name == 'posix':  # For macOS and Linux
        subprocess.call(['open', path])
def scan_drives():
    drives = []
    # print("sssssssssss",os.name)
    if os.name == 'nt':  # For Windows
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    elif os.name == 'posix':  # For macOS and Linux
        drives = ["/"]
    print("sssssssss",drives)
    for drive in drives:
        von_path = os.path.join(drive, "Von")
        if os.path.exists(von_path) and os.path.isdir(von_path):
            print(f"Found 'Von' folder in {drive}")
            open_folder(von_path)
            return von_path
        
    print("Folder 'Von' not found in any drive root.")
    return None
if __name__ == "__main__":
    scan_drives()
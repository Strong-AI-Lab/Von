import os
import subprocess
import sys
import socket
import signal


# --- Find Local IP Address ---
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Use a non-routable address for network detection
        s.connect(("8.8.8.8", 80))
        IP = s.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        s.close()
    return IP


OLLAMA_HOST_ADDRESS = get_local_ip()
OLLAMA_PORT = 11434

print(f"Detected local IP address: {OLLAMA_HOST_ADDRESS}")
print(
    f"Setting OLLAMA_HOST environment variable for this process: {OLLAMA_HOST_ADDRESS}:{OLLAMA_PORT}"
)

ollama_env = os.environ.copy()
ollama_env["OLLAMA_HOST"] = f"{OLLAMA_HOST_ADDRESS}:{OLLAMA_PORT}"

ollama_executable = "ollama"

print(f"Starting Ollama server using executable: {ollama_executable}")
print("Press Ctrl+C to stop the server.")

process = None
try:
    process = subprocess.Popen(
        [ollama_executable, "serve"],
        env=ollama_env,
    )
    process.wait()

except FileNotFoundError:
    print(f"Error: '{ollama_executable}' command not found.")
    print(
        "Please ensure Ollama is installed and its executable is in your system's PATH"
    )
    print(
        f"or provide the full path to the executable in the script (e.g., '{sys.executable} {sys.argv[0]}')."
    )
except KeyboardInterrupt:
    print("\nCtrl+C detected. Shutting down Ollama server...")
    if process:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=10)
        if process.poll() is None:
            print("Ollama server did not shut down gracefully, forcing termination.")
            process.terminate()
            process.wait()
    print("Ollama server stopped.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
    if process and process.poll() is None:
        process.terminate()

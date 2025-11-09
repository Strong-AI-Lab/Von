import os
import subprocess
import sys
import signal

# --- Configuration ---
# Set this to '0.0.0.0' to listen on all available network interfaces (local LAN access)
# Or set to your server's specific local IP address (e.g., 'your-local-ip')
# DO NOT set this to a public internet IP unless you understand the significant security risks!
OLLAMA_HOST_ADDRESS = '0.0.0.0'
OLLAMA_PORT = 11434 # Default Ollama port

# --- Script Logic ---
print(f"Setting OLLAMA_HOST environment variable for this process: {OLLAMA_HOST_ADDRESS}:{OLLAMA_PORT}")

# Set the environment variable for the subprocess
# Note: This only affects the process started by this script.
# For persistent system-wide setting, use the OS-specific methods described previously.
ollama_env = os.environ.copy()
ollama_env['OLLAMA_HOST'] = f'{OLLAMA_HOST_ADDRESS}:{OLLAMA_PORT}'

# Find the ollama executable
ollama_executable = 'ollama'
# On some systems, you might need the full path, e.g., '/usr/local/bin/ollama'
# You can try finding it:
# try:
#     ollama_executable = subprocess.check_output(['which', 'ollama']).strip().decode()
# except:
#     print("Could not find 'ollama' executable in PATH. Please specify the full path in the script.")
#     sys.exit(1)


print(f"Starting Ollama server using executable: {ollama_executable}")
print("Press Ctrl+C to stop the server.")

# Use Popen to run the ollama serve command
# This allows the Python script to stay running and manage the subprocess
process = None
try:
    process = subprocess.Popen(
        [ollama_executable, 'serve'],
        env=ollama_env,
        # Optionally redirect stdout/stderr if you don't want it in the parent terminal
        # stdout=subprocess.PIPE,
        # stderr=subprocess.PIPE
    )

    # Wait for the process to finish (which it won't unless stopped externally)
    process.wait()

except FileNotFoundError:
    print(f"Error: '{ollama_executable}' command not found.")
    print("Please ensure Ollama is installed and its executable is in your system's PATH")
    print(f"or provide the full path to the executable in the script (e.g., '{sys.executable} {sys.argv[0]}').")
except KeyboardInterrupt:
    print("\nCtrl+C detected. Shutting down Ollama server...")
    if process:
        process.send_signal(signal.SIGINT) # Send interrupt signal
        process.wait(timeout=10) # Wait for a few seconds for it to exit gracefully
        if process.poll() is None: # If it's still running, force kill
             print("Ollama server did not shut down gracefully, forcing termination.")
             process.terminate() # or process.kill()
             process.wait()
    print("Ollama server stopped.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
    if process and process.poll() is None:
         process.terminate()

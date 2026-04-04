import sys

try:  # Optional dependency; script provides guidance if missing
    import ollama  # type: ignore
except Exception as _import_err:  # pragma: no cover - environment dependent
    print(
        "[WARN] 'ollama' package not installed. Install with 'pip install ollama' to use this script.",
        file=sys.stderr,
    )
    ollama = None  # type: ignore

# --- Configuration ---
# Set this to the IP address of the server machine.
# Use the local LAN IP (e.g., 'your-server-ip') if on the same network.
# Use the public IP address (with port forwarding configured) if connecting over the internet (less secure).
# Include the port if it's not the default 11434.
OLLAMA_SERVER_URL = "http://your-remote-server:11434"  # <<< CHANGE THIS

if __name__ == "__main__":
    # --- Script Logic ---
    print(f"Attempting to connect to Ollama server at: {OLLAMA_SERVER_URL}")

    try:
        if ollama is None:
            raise RuntimeError("ollama library not available")
        # Create an Ollama client instance pointing to the remote server URL
        # The ollama-python library automatically respects the OLLAMA_HOST env var
        # BUT explicitly setting the host in the client constructor is clearer for a script.
        client = ollama.Client(host=OLLAMA_SERVER_URL)

        # Test connection by listing models
        print("\nFetching available models...")
        models = client.list()

        if models["models"]:
            print("Successfully connected to Ollama server.")
            print("Raw models response:", models)  # Print the raw response
            print("Available models:")
            for model_item in models[
                "models"
            ]:  # Iterate through the list of model details
                # Try to get 'name', then 'model'. If both fail, use a placeholder.
                model_identifier = model_item.get("name")
                if not model_identifier:
                    model_identifier = model_item.get(
                        "model", f"Identifier missing for item: {model_item}"
                    )
                print(f"- {model_identifier}")

            # Example: Run a simple completion
            if models[
                "models"
            ]:  # Ensure list is not empty before trying to access its first element
                first_model_item = models["models"][0]
                model_to_use = first_model_item.get("name")  # Try 'name' first
                if not model_to_use:
                    model_to_use = first_model_item.get("model")  # Fallback to 'model'

                if model_to_use:
                    print(f"\nRunning a completion using model '{model_to_use}'...")

                    prompt = "Tell me a very short story about a cat."
                    print(f"Prompt: '{prompt}'")

                    response = client.generate(
                        model=model_to_use, prompt=prompt, stream=False
                    )  # Set stream=False for simplicity

                    print("\nResponse:")
                    print(response["response"])
                else:
                    print(
                        f"Could not determine model to use for completion. Details of the first model: {first_model_item}",
                        file=sys.stderr,
                    )

        else:
            print(
                "Successfully connected to Ollama server, but no models are available."
            )
            print(
                "Please pull a model on the server machine using 'ollama pull <model_name>'"
            )

    except Exception as e:  # Consolidated error handling
        if ollama is not None and getattr(ollama, "ResponseError", None) and isinstance(e, ollama.ResponseError):  # type: ignore[attr-defined]
            print(f"Ollama API returned an error: {e}", file=sys.stderr)
            print(
                "Ensure the model exists on the server and the server is running correctly.",
                file=sys.stderr,
            )
        else:
            print(
                f"Failed to connect to Ollama server at {OLLAMA_SERVER_URL}",
                file=sys.stderr,
            )
            print(f"Error: {e}", file=sys.stderr)
            print("\nPlease check the following:", file=sys.stderr)
            print(
                "1. The server script 'start_ollama_remote.py' is running on the server machine.",
                file=sys.stderr,
            )
            print(
                "2. The server machine's firewall allows incoming TCP connections on port 11434.",
                file=sys.stderr,
            )
            print(
                f"3. The IP address '{OLLAMA_SERVER_URL}' is correct and reachable from this client machine.",
                file=sys.stderr,
            )
            if "Connection refused" in str(e):
                print(
                    "   (Error: Connection refused suggests a firewall issue or Ollama not running/listening on that address)",
                    file=sys.stderr,
                )
            elif "timed out" in str(e):
                print(
                    "   (Error: Connection timed out suggests a network routing or firewall issue)",
                    file=sys.stderr,
                )

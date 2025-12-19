try:  # Optional dependency
    import ollama  # type: ignore
except ImportError:  # pragma: no cover
    ollama = None  # type: ignore
import logging
import re
import requests


logger = logging.getLogger(__name__)

def ollama_generate(prompt: str, context=None, model: str = "granite3.3:2b") -> str:
    """
    Generate a response using the Ollama LLM.
    """
    # Pull model if needed
    if ollama is None:
        return "Ollama library not installed. Install 'ollama' to enable local model generation."
    ollama.pull(model)

    try:
        # Handle context if provided
        if context and isinstance(context, list):
            messages = context.copy()
            messages.append({"role": "user", "content": prompt})
        else:
            messages = [{"role": "user", "content": prompt}]

        response = ollama.chat(model=model, messages=messages)

        try:
            return response['message']['content']  # type: ignore[index]
        except Exception:
            # Normalise all fallback shapes deterministically to str
            if isinstance(response, dict):
                msg = response.get('message')
                if isinstance(msg, dict):
                    inner = msg.get('content')
                    if isinstance(inner, str):
                        return inner
                    return str(inner)
                content = response.get('content')
                if isinstance(content, str):
                    return content
                if content is not None:
                    return str(content)
            # Last resort: regex extraction or full repr
            response_str = str(response)
            match = re.search(r"content=['\"](.*?)['\"]", response_str, re.DOTALL)
            return match.group(1) if match else response_str
    except Exception as e:
        return f"Error generating response: {str(e)}"

def list_local_ollama_models_details(host: str = "http://localhost:11434") -> list:
    """
    Lists locally available Ollama models using the Ollama API.
    """
    try:
        response = requests.get(f"{host}/api/tags")
        response.raise_for_status()
        models = response.json().get("models", [])
        return models
    except requests.RequestException as e:
        logger.warning("Error contacting Ollama API: %s", e)
        return []

def extract_model_names(models: list) -> list:
    """
    Extracts the 'name' field from a list of model dictionaries.
    """
    return [model.get('name') for model in models if isinstance(model, dict) and 'name' in model]

def list_local_ollama_models() -> list:
    """
    Lists locally available Ollama models and extracts their names.
    """
    try:
        if ollama is None:
            return []
        models = list_local_ollama_models_details()
        model_names = extract_model_names(models)
        return model_names
    except Exception as e:
        logger.warning("Error listing local Ollama models: %s", e)
        return []


if __name__ == "__main__":
    print("utils_ollama.py is being executed.")
    # Test the list_local_ollama_models function
    print("Testing list_local_ollama_models...")
    try:
        # Use the new list_local_ollama_models function
        model_names = list_local_ollama_models()
        print(f"Extracted model names: {model_names}")

        # Check for the presence of 'granite3.3:2b'
        if "granite3.3:2b" in model_names:
            print("Model 'granite3.3:2b' is available.")
        else:
            print("Model 'granite3.3:2b' is not available.")
    except Exception as e:
        print(f"Error during list_local_ollama_models test: {e}")

# src/backend/languagemodels/openai_client.py

import os
from openai import OpenAI

class OpenAIClient:
    def __init__(self, api_key_env_var="OPENAI_API_KEY"):
        self.api_key = os.getenv(api_key_env_var)
        if not self.api_key:
            raise ValueError(f"Environment variable {api_key_env_var} not set.")
        self.client = OpenAI(api_key=self.api_key)

    def list_models(self):
        """Lists available OpenAI models."""
        try:
            models = self.client.models.list()
            # Filter for GPT models that are likely to be useful for generation
            return [model.id for model in models if "gpt" in model.id.lower()]
        except Exception as e:
            print(f"Error listing OpenAI models: {e}")
            return []

    def generate(self, prompt, model="gpt-4"):
        """Generates a response from the OpenAI API."""
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Error generating response from OpenAI: {e}")
            return f"Error: {e}"

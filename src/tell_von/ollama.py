#ollama is installed from https://ollama.com/download/linux

#If your system is Linux, please install with: curl -fsSL https://ollama.com/install.sh | sh

#Using "ollama serve" to start


#An example
from ollama import Client
import ollama

ollama.pull("qwen:0.5b")
client = Client(host='http://localhost:11434')
response = client.chat(model='qwen:0.5b', messages=[
  {
'role': 'user',
'content': 'Why is the sky blue?',
  },
])
print(response)
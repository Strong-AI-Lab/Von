import os
from openai import OpenAI

useollama=True

if (useollama):
    the_model = 'llama3'
    client = OpenAI(
        base_url = 'http://localhost:11434/v1',
        api_key='ollama', # required, but unused
    )
else:
    # Your OpenAI API key
    api_key = os.getenv("OpenAI_API_KEY") 
    the_model = 'gpt-4o'
    client = OpenAI(api_key=api_key)


# from flask import Flask, redirect, render_template, request, url_for

#https://flask.palletsprojects.com/en/2.3.x/quickstart/
#python -m flask run

# for models see https://platform.openai.com/docs/deprecations
# for cost see https://openai.com/api/pricing/

#api details https://platform.openai.com/docs/guides/text-generation

#openai.api_key = os.getenv("OPENAI_API_KEY")
# Configure the API key from your OpenAI account

def ask_gpt4(prompt_text, system_prompt=None):
    try:
        # Building the prompt with an optional system message
        full_prompt = f"{system_prompt}\n\n{prompt_text}" if system_prompt else prompt_text

        # Sending the prompt to the GPT-4 model
        response = client.chat.completions.create( model=the_model,  # Use GPT-4's engine identifier, update if necessary
            # messages=[
            #     {
            #         "role": "user",
            #         "content": "How do I output all files in a directory using Python?",
            #     },
            # ],                                     
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": prompt_text,
                },
            ],
            max_tokens=150  # Adjust based on how long you expect the response to be
        )

        # Extracting and returning the text response
        return response.choices[0].message.content
    except Exception as e:
        print(f"An error occurred: {e}")
        return None




response = client.chat.completions.create(
  model="llama3",
  messages=[
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Who won the world series in 2020?"},
    {"role": "assistant", "content": "The LA Dodgers won in 2020."},
    {"role": "user", "content": "Where was it played?"}
  ]
)
print(response.choices[0].message.content)



# app = Flask(__name__)

# @app.route("/", methods=("GET", "POST"))
# def index():
#     if request.method == "POST":
#         task = request.form["task"]
#         response = openai.Completion.create(
#             model="text-davinci-003",
#             prompt=generate_prompt(task),
#             temperature=0.6,
#             max_tokens=1000
#         )
#         return redirect(url_for("index", result=response.choices[0].text))

#     result = request.args.get("result")
#     return render_template("index.html", result=result)

# def generate_prompt(task):
#     return """Suggest a small number of initial steps to take to carry out a task.

# Task: Empty Rubbish Bin
# Steps: Open bin, take out bag, tie bag, put new bag in bin, close bin, take bag to collection point
# Task: Wash Hands
# Steps: Go to washbasin, turn on hot and cold tap part way, if steaming turn down hot, test temperature with finger, turn down hot until comfortable, wet hands, rub soap with hands under tap, replace soap, rinse hands, turn off taps
# Task: {}
# Steps:""".format(
#         task.capitalize()
#     )


# def generate_prompt_a(animal):
#     return """Suggest three names for an animal that is a superhero.

# Animal: Cat
# Names: Captain Sharpclaw, Agent Fluffball, The Incredible Feline
# Animal: Dog
# Names: Ruff the Protector, Wonder Canine, Sir Barks-a-Lot
# Animal: {}
# Names:""".format(
#         animal.capitalize()
#     )

# print ("hello world")
# index()

# def send_prompt(prompt):
#     # Replace 'YOUR_GPT4_API_ENDPOINT' with the actual API endpoint for GPT-4
#     api_endpoint = 'YOUR_GPT4_API_ENDPOINT'

#     # Set the headers and payload for the API request
#     headers = {'Content-Type': 'application/json'}
#     payload = {'prompt': prompt}

#     try:
#         # Send the API request
#         response = requests.post(api_endpoint, headers=headers, json=payload)

#         # Check if the request was successful
#         if response.status_code == 200:
#             # Extract the response from the API response
#             response_data = response.json()
#             gpt4_response = response_data['response']

#             return gpt4_response
#         else:
#             print('Error: Failed to send prompt to GPT-4')
#     except requests.exceptions.RequestException as e:
#         print('Error: Failed to connect to GPT-4 API')
#         print(e)

# # Example usage
# prompt = "Hello, GPT-4!"
# response = send_prompt(prompt)
# print(response)



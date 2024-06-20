"""
This file contains the configuration settings for the 
vonUKU application, Michael's personal von class agent related to vonNeumarkt.
"""
# This file is currently not really used. It is just a placeholder for future use.
config = {
    'folder_id': 'your_folder_id',
    'secret_key': 'your_secret_key',
    'styling_choice': 'your_styling_choice'
}

# Save the config to a file
with open('config.txt', 'w') as file:
    for key, value in config.items():
        file.write(f'{key}={value}\n')
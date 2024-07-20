"""
This file contains the configuration settings for the 
von application. It is a placeholder for future use. 
The configuration settings are currently loaded from respective environment variables.
This file is probably not a good idea. Consider deleting it.
"""
# This file is currently not really used. It is just a placeholder for future use.
config = {
    'folder_id': 'your_folder_id', #these are currently loaded from environment variables
    #'secret_key': 'your_secret_key',
    'styling_choice': 'your_styling_choice'
}

# Save the config to a file
with open('config.txt', 'w') as file:
    for key, value in config.items():
        file.write(f'{key}={value}\n')
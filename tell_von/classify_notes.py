import os
import json
from datetime import datetime
from tell_von.llmconnect import ask_llm
from tell_von.googledrive import iterate_files_in_folder, get_file_content, get_default_folder_id, get_service

def classify_file(file_content):
    """
    Classifies notes based on the file content. It uses a list of classes derived from a sample (or all at first) of the notes.

    Args:
        file_content (str): The content of the file.

    Returns:
        A single class for the note.
    """
    return "class"
    # # Use GPT-4 to generate follow-up questions
    # system_prompt = """You are an expert at analyzing the content and structure of 
    #     files. Can you generate a list of follow-up questions based on the following file content? Each question should be on a separate line and start with a dash (-).
    #     The file content is:
    #     """
    # user_prompt = file_content
    # response = ask_gpt4(user_prompt, system_prompt)

    # # Parse the response and extract the follow-up questions
    # questions = []
    # for line in response.splitlines():
    #     if line.startswith("- "):
    #         questions.append(line[2:])

    # if questions == [] :
    #     return None
    # else: 
    #     return questions


def get_file_timestamp(file_id):
    """Get the timestamp of a file in Google Drive."""
    file_metadata = get_service().files().get(fileId=file_id, fields='modifiedTime').execute()
    modified_time = file_metadata['modifiedTime']
    return datetime.strptime(modified_time, '%Y-%m-%dT%H:%M:%S.%fZ')

def is_newer_than_json(file_id, json_file_path):
    """Check if the note file is newer than the local JSON file."""
    file_timestamp = get_file_timestamp(file_id)
    json_file_timestamp = datetime.fromtimestamp(os.path.getmtime(json_file_path))
    return file_timestamp > json_file_timestamp

def save_json_to(json_file_path, json_content, indent=4):
    """Save the note contents to a local JSON file."""
    with open(json_file_path, 'w') as json_file:
        json.dump(json_content, json_file, indent=indent)

import os
import json

def build_class_list():
    """
    Builds a class list by iterating through files in a folder and saving relevant information to a JSON file.

    Args:
        None

    Returns:
        None
    """
    dir_path = "ignore"  
    json_file_name = "notes.json"  
    json_file_path = os.path.join(dir_path, json_file_name)
    print (json_file_path)

    folder_id = get_default_folder_id()
    count = 0

    # Create the JSON file if it doesn't exist
    if not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True, mode=511)
    if not os.path.exists(json_file_path):
        with open(json_file_path, 'w') as json_file:
            json_data = []
            json.dump(json_data, json_file, indent=4)
            count = 0
    else:
        with open(json_file_path, 'r') as json_file:
            json_data = json.load(json_file)
            count = len(json_data)       


    # Iterate through files in the folder
    #TODO: Make this actually add new records into the JSON file, not append them. At present, it just keeps adding new records to the JSON file.
    # https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-79
    for file in iterate_files_in_folder(folder_id):
        # Check if the file is newer than the JSON file or in test mode
        if is_newer_than_json(file['id'], json_file_path) or test_mode == True:
            count += 1
            file_content = get_file_content(file['id'])
            file_timestamp = get_file_timestamp(file['id'])
            file_record = {'timestamp': file_timestamp.isoformat(), 'filename': file['name'], 'content': file_content}
            json_data.append(file_record)
            print(f"{count} saving to json record from {file_record['filename']}")

    # Save the modified JSON file
    save_json_to(json_file_path,json_data)
    print(f"Saved {count} records to {json_file_path}")

def deduplicate_json(json_array, field_to_sort_on=None):

    # Convert the JSON array to a list of dictionaries, making a deep copy to avoid modifying the original data
    list_of_dicts = json.loads(json.dumps(json_array))

    # Use a dictionary comprehension to remove duplicates
    # This will keep the first occurrence of each record
    unique_records = [dict(t) for t in set(tuple(d.items()) for d in list_of_dicts)]
    sorted_unique_records=unique_records if field_to_sort_on is None else sorted(unique_records, key=lambda k:k[field_to_sort_on])    
    # Convert the list of unique dictionaries back to a JSON array
    json_array_unique = json.loads(json.dumps(sorted_unique_records))
    #json_array_unique = sorted_unique_records

    print(json_array_unique)
    return json_array_unique


if __name__ == "__main__":
    test_mode = True
    build_class_list()
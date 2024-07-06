import os
import json
from datetime import timezone
from datetime import datetime
from vonlib.llmconnect import ask_llm
from vonlib.googledrive import iterate_files_in_folder, get_file_content, get_default_folder_id, get_service,save_to_drive, save_to_drive_as_google_doc
import re
# Split before lines containing day/month/year patterns, 
# where day month and year are (possibly 0 padded) integers
import re

#Misc Notes is a file in the GDrive file that allows notes to be added, roughly, without the tell_von system.
# it may be a bad idea.
misc_notes_file_name = 'misc_inputs'

verbose = True
def split_before_date_patterns(file_content):
    # Regular expression pattern to match day/month/year with possible leading zeros
    date_pattern = r'((?<!\d)0?\d{1,2}[/\-.]0?\d{1,2}[/\-.]\d{4}(?!\d))'
    
    # Split the content, retaining the dates in the result
    parts = re.split(date_pattern, file_content)
    
    # Initialize an empty list to hold the JSON records
    partList = []
    
    # Temporary variable to hold the current date
    time_stamp = None
    
    # Iterate through the parts to create JSON records
    for i, part in enumerate(parts):
        if re.match(date_pattern, part):  # If the part is a date
            # Extract the date and store it in the the_date variable
            date_match = re.search(date_pattern, part)
            date = date_match.group(1)
            date_parts = re.split(r'[\/\-.]', date)  # Split the date into day, month, and year")
            date_string= f"{date_parts[0]}/{date_parts[1]}/{date_parts[2]}"
            time_stamp = datetime.strptime(date_string, "%d/%m/%Y").isoformat()
        else:  # Even index, regular content
            if time_stamp:  # If there's a current date, create a record
                record = {'timestamp': time_stamp, 'content': part.strip()}
               # record = {"date": time_stamp, "content": part.strip()}
                partList.append(record)
                time_stamp = None  # Reset the current date for the next record
    
    return partList



def misc_notes_to_timestamped_json():
    """
    Converts the contents of the Misc Notes file to a JSON file with timestamps.

    Args:
        json_data (list): The JSON data to append the Misc Notes to.

    Returns:
        int: The number of new records added to the JSON file
    """
    # Get the content of the Misc Notes file
    notes_added: int = 0 
    folder_id = get_default_folder_id()
    json_data = []
    counter=0
    for file in iterate_files_in_folder(folder_id):
        counter+=1
        filename=file['name']
        index = filename.find("2024")
        if index == -1:
            print (">>>>>", filename, misc_notes_file_name)
        # else:
        #     print (filename," ",counter," has 2024 at ",index)
        if file['name'] == misc_notes_file_name:
            file_content = get_file_content(file['id'])
            print (f"Found {misc_notes_file_name} with {len(file_content)} characters")
            break
    else: #didn't find the file
        # create it
        new_id= save_to_drive_as_google_doc(file_content="Misc Notes go in this file if you don't have the app - use \nDD/MM/YYYY dates for separators\n\n", 
                      file_name=misc_notes_file_name)
        print (f"Created {misc_notes_file_name} with id {new_id} in folder {folder_id}") 
        return 0 #no new records added


    # # Split the content into lines and remove empty lines
    # lines = file_content.splitlines()
    # lines = [line for line in lines if line.strip() != '']

    # Create a list of dictionaries with timestamps and the content of each line
    json_data = []
    for record in split_before_date_patterns(file_content):
        json_data.append(record)

    return json_data

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

#TODO: Consider moving this to the googledrive.py file.
def get_gdrive_file_timestamp(file_id):
    """Get the timestamp of a file in Google Drive."""
    file_metadata = get_service().files().get(fileId=file_id, fields='modifiedTime').execute()
    modified_time = file_metadata['modifiedTime']
    modified_time_datetime = datetime.strptime(modified_time, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) 
    return modified_time_datetime
#TODO: Consider moving this to a windowsdrive.py file. (doesn't exist yet)
def get_timestamp_from_path(file_path):
    """Get the timestamp of a file from its path."""
    modified_time = os.path.getmtime(file_path)
    #TODO: Make the date comparison reliable. The UTC timestamp is not to be relied on. If necessary, save the json file to Google Drive.
    modified_time_datetime = datetime.fromtimestamp(modified_time, tz=timezone.utc)
    return modified_time_datetime

def is_newer_than_json(file_id, json_file_path):
    """Check if the note file is newer than the local JSON file."""
    file_timestamp = get_gdrive_file_timestamp(file_id)
    json_file_timestamp = get_timestamp_from_path(json_file_path)
    comparison = file_timestamp > json_file_timestamp
    return comparison

def save_json_to(json_file_path, json_content, indent=4):
    """Save the note contents to a local JSON file."""
    with open(json_file_path, 'w') as json_file:
        json.dump(json_content, json_file, indent=indent)

import os
import json

def build_class_list(test_mode=False):
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
    skip_count=0
    new_count=0

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
            print(f"Loaded {count} records from {json_file_path}")   

   

    # Iterate through files in the folder
    #TODO: Make this actually add new records into the JSON file, not append them.
    #  At present, it just keeps adding new records to the JSON file.
    # https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-79
    for file in iterate_files_in_folder(folder_id):
        file_timestamp = get_gdrive_file_timestamp(file['id']).isoformat()
        # Check if the file is newer than the JSON file or in test mode
        if (file['name'] == misc_notes_file_name):
             # Get any new notes from the Misc Notes file
            misc_json_data=misc_notes_to_timestamped_json()
            new_count+=len(misc_json_data)
            print(f"Added {new_count} records from {misc_notes_file_name}")
            #print(f"Skipping {file['name']} from {file_timestamp} because it is the Misc Notes file")
            continue
        if is_newer_than_json(file['id'], json_file_path) or test_mode == True:
            count += 1
            new_count+=1
            file_content = get_file_content(file['id'])
            file_record = {'timestamp': file_timestamp, 'filename': file['name'], 'content': file_content}
            json_data.append(file_record)
            print(f"{count} saving to json record from {file_record['filename']}")
        elif verbose:
            skip_count+=1
            print(f"{skip_count}: Skipping {file['name']} from {file_timestamp} becasuse it is not newer than"\
                  f" {json_file_path} from {get_timestamp_from_path(json_file_path)} ")

    # Save the modified JSON file
    if (new_count >0):
        save_json_to(json_file_path,deduplicate_json(json_data,field_to_sort_on='timestamp'))
        print(f"Saved {count} records to {json_file_path}")
    else:
        print(f"No new records to save to {json_file_path}")

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

    #print(json_array_unique)
    return json_array_unique


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

    #print(json_array_unique)
    return json_array_unique


if __name__ == "__main__":
    build_class_list(test_mode=False)
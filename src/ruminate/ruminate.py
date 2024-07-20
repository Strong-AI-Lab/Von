from vonlib.googledrive import iterate_files_in_folder, get_file_content, get_default_folder_id, trash_file
from classify_notes import classify_file
from vonlib.llmconnect import ask_llm
import re


def check_file_is_test_file(file_content):
    """
    Determines if the given file content is from a test file.

    Args:
        file_content (str): The content of the file.

    Returns:
        bool or None: True if the file is likely a test file, False if it is a genuine input, None if it's uncertain.
    """

    system_prompt = """
    You are an expert in analyzing file content and structure. Your task is to determine if the following string is from a test file. Return 'True' if the content is likely produced during testing, 'False' if it is a genuine input, and 'Unknown' if it is difficult to tell.

    Guidelines:
    1. Assume files are non-test unless there are clear indications they are test files.
    2. Flag files as test files if the content is nonsensical or explicitly mentions testing. Merely mentioning 'test' is not enough.
    3. Consider any coherent extended text as a non-test file.

    Examples:
    - Real file: 'An early background function for vonUKU can be recognizing and deleting (binning) test cases from portal development.'
    - Test files: 'test again with the button', 'Test multi line input'
    - Ambiguous: 'Does the text input area work now?' (This was a test file, but it's not obvious.)

    Evaluate the following
    """
    user_prompt = file_content
    response = ask_llm(user_prompt, system_prompt)
    # print(f"GPT-4 says: {response} caps {response.capitalize()}")
    if 'True' in response.capitalize():
        return True
    else:
        if 'False' in response.capitalize():
            return False
        else: #it's uncertain, return None
            return None


# response = check_file_is_test_file("This is a test file")
# print("Is test file:", response)


# write a function that reads the file content and works out what followup questions should be asked or what next actions should be taken

# @todo replace this with the classify_notes.py method and have that decide e.g. that a file is a test file
# @todo learn how to make test cases
 
def analyze_file_content(file_content):
    """
    Analyzes the file content and determines the next steps.

    Args:
        file_content (str): The content of the file.

    Returns:
        tuple: A tuple containing the next steps and any additional information.
    """

    # Check if the file is a test file
    is_test_file = check_file_is_test_file(file_content)
    if is_test_file == True:
        # Next step: Delete the file
        next_step = "delete"
        additional_info = None
    elif (is_test_file == False) & ((file_class := classify_file(file_content)) != None) :
        next_step = "classify_file"
        additional_info = file_class
    elif (questions := generate_followup_questions(file_content)) != None:
        # Next step: Ask follow-up questions
        next_step = "ask_questions"
        additional_info = questions
    else:
        # Next step: Uncertain, refer to human for review
        next_step = "uncertain"
        additional_info = None

    return next_step, additional_info


def generate_followup_questions(file_content):
    """
    Generates follow-up questions based on the file content.

    Args:
        file_content (str): The content of the file.

    Returns:
        list: A list of follow-up questions.
    """

    # Use GPT-4 to generate follow-up questions
    system_prompt = """You are an expert at analyzing the content and structure of 
        files. Can you generate a list of follow-up questions based on the following file content? Each question should be on a separate line and start with a dash (-).
        The file content is:
        """
    user_prompt = file_content
    response = ask_llm(user_prompt, system_prompt)

    # Parse the response and extract the follow-up questions
    questions = []
    for line in response.splitlines():
        if line.startswith("- "):
            questions.append(line[2:])

    if questions == [] :
        return None
    else: 
        return questions


def ruminate(folder_id=get_default_folder_id()):
    """
    Iterates through the files in the specified folder and performs analysis on their content.

    Args:
        folder_id (str): The ID of the folder to iterate through. Defaults to the default folder ID.

    Returns:
        None
    """

    file_limit = 3
    file_counter = 0

    for file in iterate_files_in_folder(folder_id):
        if file_counter >= file_limit:
            break

        id = file.get('id')
        file_content = get_file_content(id)
        flat_file_content = re.sub(r'\s+', ' ', file_content)
        next_step, additional_info = analyze_file_content(file_content)

        if next_step == "delete":
            print('Pretending to Delete : ',next_step,' for File Name:', file.get('name'), ' File ID:', file.get('id'),'---', flat_file_content)
        elif next_step == "classify-task": 
            print('Classify Task: ',additional_info,' for File Name:', file.get('name'), ' File ID:', file.get('id'),'---', flat_file_content)  
        elif next_step == "ask_questions":
            print('Follow-up Questions: ',additional_info,' for File Name:', file.get('name'), ' File ID:', file.get('id'),'---', flat_file_content)
        else:
            print(".")
        
        file_counter += 1



if __name__ == "__main__":
    ruminate()

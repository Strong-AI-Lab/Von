import unittest
#from unittest.mock import patch
from tell_von.gpt4connect import ask_gpt4

class TestGpt4Connect(unittest.TestCase):

 #   @patch('gpt4connect.OpenAI')
    def test_ask_gpt4_1(self): #, mock_openai):
        # Mock the OpenAI client
        #mock_client = mock_openai.return_value
        #mock_client.chat.completions.create.return_value.choices[0].message.content = "Response from GPT-4"

        # Test case 1: Prompt with system message
        prompt_text = "How do I output all files in a directory using Python?"
        system_prompt = "You are an expert on filesystem programming"
        response = ask_gpt4(prompt_text, system_prompt)
        print(f"Test 1 Response: {response}")
        expected_response = "os"
        self.assertIn(expected_response, response)
        expected_response = "module"
        self.assertIn(expected_response, response)


    def test_ask_gpt4_2(self):#, mock_openai):
        # Mock the OpenAI client
        #mock_client = mock_openai.return_value
        #mock_client.chat.completions.create.return_value.choices[0].message.content = "Response from GPT-4"

        # Test case 2: Prompt without system message
        prompt_text = "Who won the world series in 2020?"
        expected_response = "Dodgers"
        response = ask_gpt4(prompt_text)
        print(f"Test 2 Response: {response}")
        self.assertIn(expected_response,response )

if __name__ == '__main__':
    unittest.main()
import unittest
import json
from tell_von.classify_notes import classify_file, deduplicate_json, split_before_date_patterns

# This is your JSON array
json_array = json.loads(json.dumps([
    {"a": "thea", "b": "theb"},
    {"a": "thea", "b": "theb"},
    {"a": "thea2", "b": "theb1"},
    {"a": "thea1", "b": "theb2"}
]))
json_array_dedup = json.loads(json.dumps([
    {"a": "thea", "b": "theb"},
    {"a": "thea2", "b": "theb1"},
    {"a": "thea1", "b": "theb2"}
]))
json_array_dedup_sort = json.loads(json.dumps([
    {"a": "thea", "b": "theb"},
    {"a": "thea1", "b": "theb2"},
    {"a": "thea2", "b": "theb1"}
]))
json_array_nop = json.loads(json.dumps([
    {"a": "thea", "b": "theb"},
    {"a": "thea1", "b": "theb1"},
    {"a": "thea2", "b": "theb2"}
]))

class TestClassifyNotes(unittest.TestCase):
    def test_dedupliate_json_positive(self):
        # Test positive cases here
        self.assertEqual(len(deduplicate_json(json_array)), len(json_array_dedup))
        self.assertEqual(len(deduplicate_json(json_array,"a")), len(json_array_dedup_sort))
        # Add more positive test cases

    def test_deduplicate_json_note_negative(self):
        # Test negative cases here
        self.assertEqual(len(deduplicate_json(json_array_nop)), len(json_array_nop))
        self.assertEqual(len(deduplicate_json(json_array_nop,"a")), len(json_array_nop))
        # Add more negative test cases

    testString="""misc inputs to tell Von

21/06/2024
Who is Mike?

21/06/2024 
Plan trip to Europe.

test
    more complex format
    numbers 22 (too bad if there are dates)

21/06/2024
Connections connections!
"""
    def test_split_before_date_patterns_positive(self):
        # Test positive cases here
        result=split_before_date_patterns(self.testString)
        self.assertEqual(len(result), 3)
        # Add more positive test cases
        result=split_before_date_patterns("")
        self.assertEqual(len(result), 0)

        result=split_before_date_patterns("21/06/2024")
        self.assertEqual(len(result), 1) #if there is a date, accept it anyway, even if nothing else
        
        result=split_before_date_patterns("21/06/2024\n")
        self.assertEqual(len(result), 1)
        
        result=split_before_date_patterns("21/06/2024\n\n") 
        self.assertEqual(len(result), 1)
        
        result=split_before_date_patterns("This file has no dates\n")
        self.assertEqual(len(result), 0) #ignore files with no dates




if __name__ == '__main__':
    unittest.main()
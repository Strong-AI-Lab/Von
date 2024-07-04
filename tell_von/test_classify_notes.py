import unittest
import json
from tell_von.classify_notes import classify_file, deduplicate_json

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



if __name__ == '__main__':
    unittest.main()
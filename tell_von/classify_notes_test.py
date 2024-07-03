import unittest
from tell_von.classify_notes import classify_note, deduplicate_json

# This is your JSON array
json_array = [
    {"a": "thea", "b": "theb"},
    {"a": "thea", "b": "theb"},
    {"a": "thea2", "b": "theb1"},
    {"a": "thea1", "b": "theb2"}
]

json_array_dedup = [
    {"a": "thea", "b": "theb"},
    {"a": "thea2", "b": "theb1"},
    {"a": "thea1", "b": "theb2"}
]
json_array_dedup_sort = [
    {"a": "thea", "b": "theb"},
    {"a": "thea1", "b": "theb2"},
    {"a": "thea2", "b": "theb1"}
]

json_array_nop = [
    {"a": "thea", "b": "theb"},
    {"a": "thea1", "b": "theb1"},
    {"a": "thea2", "b": "theb2"}
]
class TestClassifyNotes(unittest.TestCase):
    def test_dedupliate_json_positive(self):
        # Test positive cases here
        self.assertEqual(deduplicate_json(json_array), json_array_dedup)
        self.assertEqual(deduplicate_json(json_array,"a"), json_array_dedup_sort)
        # Add more positive test cases

    def test_classify_note_negative(self):
        # Test negative cases here
        self.assertEqual(deduplicate_json(json_array_nop), json_array_nop)
        self.assertEqual(deduplicate_json(json_array_nop,"a"), json_array_nop)
        # Add more negative test cases

if __name__ == '__main__':
    unittest.main()
"""Check what name types are in the database."""
from pymongo import MongoClient
import os

db_name = os.environ.get('VON_DB_NAME', 'von_db')
client = MongoClient('mongodb://localhost:27017')
db = client[db_name]

# Get all distinct name_types
distinct_types = db['text_values'].distinct('name_type')
print('All distinct name_types in text_values:')
for t in sorted(distinct_types):
    count = db['text_values'].count_documents({'name_type': t})
    print(f'  {t}: {count}')

# Get some examples of CODE type
print('\nSample CODE type entries:')
code_samples = db['text_values'].find({'name_type': 'CODE'}).limit(5)
for doc in code_samples:
    text = doc.get('text', '')[:80]
    lang = doc.get('language', 'N/A')
    print(f'  Text: {text}... Lang: {lang}')

# Check for en-NZ CODE
print('\nCODE entries with language en-NZ:')
code_en_nz = db['text_values'].count_documents({'name_type': 'CODE', 'language': 'en-NZ'})
print(f'  Count: {code_en_nz}')

code_en_nz_samples = db['text_values'].find({'name_type': 'CODE', 'language': 'en-NZ'}).limit(3)
for doc in code_en_nz_samples:
    text = doc.get('text', '')[:80]
    print(f'    {text}')

# Check for vonGUID
print('\nCODE entries with language vonGUID:')
code_vonguid = db['text_values'].count_documents({'name_type': 'CODE', 'language': 'vonGUID'})
print(f'  Count: {code_vonguid}')

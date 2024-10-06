import pytest
from web_interface.user_data import VonUser
import os

test_user={'username':'testuser', 'email':'test@testing.org', 'password':'testpassword'}

def test_set_userDB():
     VonUser.set_userDB('testDB', 'testCollection')
     assert VonUser.userdb.name == 'testDB'
     assert VonUser.usercollection.name == 'testCollection'

def test_create_user():
    VonUser.set_userDB('testDB', 'testCollection')
    user = VonUser.create_user(test_user['username'], test_user['email'], test_user['password'])

def test_init_user():
    VonUser.set_userDB('testDB', 'testCollection')
    user = VonUser.create_user(test_user['username'], test_user['email'], test_user['password'])
    
    user = VonUser(test_user['username'])
    assert user.get_username() == test_user['username']
    assert user.email == test_user['email']
    assert user.password == test_user['password'] 
    assert user.id is not None 

def test_database_structure():
    dbname=os.getenv('VON_CONFIG_DB')
    collname=os.getenv('VON_USER_COLLECTION')

    VonUser.set_userDB() # use default values
    db=VonUser.get_userDB()
    assert db.name == dbname
    coll=VonUser.get_userCollection()
    assert coll.name == collname

# def test_update_email(mock_user_collection):
#     VonUser.usercollection = mock_user_collection
#     user = VonUser('testuser', 'test@example.com')
#     user.update_email('new@example.com')
#     assert user.email == 'new@example.com'

# def test_get_user_info(mock_user_collection):
#     VonUser.usercollection = mock_user_collection
#     user = VonUser('testuser', 'test@example.com')
#     user_info = user.get_user_info()
#     assert user_info['username'] == 'testuser'
#     assert user_info['email'] == 'test@example.com'
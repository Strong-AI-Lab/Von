import pytest
from web_interface.user_data import VonUser

def test_set_userDB():
     VonUser.set_userDB('testDB', 'testCollection')
     assert VonUser.userdb.name == 'testDB'
     assert VonUser.usercollection.name == 'testCollection'

def test_init_user():
    user = VonUser('zhan')
    assert user.get_username() == 'zhan'
    assert user.email == 'vonneumarkt@gmail.com'
    assert user.id is not None

def test_database_structure():
    VonUser.set_userDB() # use default values
    db=VonUser.get_userDB()
    assert db.name == 'vonUsers'
    coll=VonUser.get_userCollection()
    assert coll.name == 'users'

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
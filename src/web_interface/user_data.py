from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash

from vonlib import database_driver as vondb

config = {}
config["USER_DB"] = "vonUsers"
config["USER_COLLECTION"] = "users"

vonuserdb = config["USER_DB"]
vonusercollection = config["USER_COLLECTION"]

class VonUser:
    vomongo = vondb.DatabaseDriver()
    userdb=None
    usercollection=None
    
    @classmethod
    def set_userDB(cls, userdb_name=vonuserdb, usercollection_name=vonusercollection):
        """
        Sets the user database collection for the class.

        This method assigns the provided user database name to the class attribute `userdb`.
        It then checks if the database exists in the `vomongo` instance. If the database does not exist,
        it attempts to create it. If the database still does not exist after the creation attempt,
        an exception is raised.

        Args:
            userdb (str): The name of the user database to set.

        Raises:
            Exception: If the database does not exist and could not be created.
        """

        user_database = cls.vomongo.get_database(userdb_name)
        if  user_database is None:
            user_database = cls.vomongo.create_database(userdb_name)
            if user_database is None:
                raise Exception(f"Database {userdb_name} does not exist and could not be created.")
        
        user_collection = user_database.get_collection(usercollection_name)
        if user_collection is None:
            user_collection=user_database.create_collection(usercollection_name)
            if user_collection is None:
                raise Exception(f"Collection {usercollection_name} does not exist and could not be created.")

        cls.userdb = user_database
        cls.usercollection = user_collection
        print(f"VonUser: DB={cls.userdb.name}, Coll={cls.usercollection.name}")

    @classmethod
    def create_user(cls, username, email, password):
        """
        Creates a new user in the user database collection.

        This method creates a new user document in the user database collection. The user document
        contains the provided username, email, and password.

        Args:
            username (str): The username of the new user.
            email (str): The email of the new user.
            password (str): The password of the new user.

        Returns:
            VonUser: A VonUser object representing the new user.
        """

        user_dict = {
            'username': username,
            'email': email,
            'password': password #should be hashed by now
        }

        cls.get_userCollection().insert_one(user_dict)
        return VonUser(username)
    
    @classmethod
    def get_userCollection(cls):
        return cls.usercollection
    
    @classmethod
    def get_userDB(cls):
        return cls.userdb

    @classmethod
    def find_by_id(cls, id):
        vu=cls.get_userCollection().find_one({"_id": ObjectId(id)})
        if vu is None:
            return None
        return VonUser(vu['username'])   #return VonUser object 
    
    @classmethod
    def find_by_username(cls, username):
        vu=cls.get_userCollection().find_one({"username": username})
        if vu is None:
            return None 
        return VonUser(username)   #return VonUser object


    def __init__(self, username):
        self.username = username
        vu=VonUser.get_userCollection().find_one({"username": username})
        if vu is None:
            raise Exception(f"User {username} not found.")  
        
        self.email = str(vu['email'])
        self.id = str(vu['_id'])
        self.password = str(vu['password'])  
        print(f"VonUser: {self.username} {self.email} {self.id} {self.password}")  #@TODO remove this debug print and the insecure password handling

    def update_email(self, new_email):
        self.email = new_email

    def get_username(self):
        return self.username
    
    def get_id(self):
        return self.id
    
    def get_password(self):
        return self.password
    
    def get_email(self):
        return self.email
    
    # def get_user_info(self):
    #     return {
    #         'username': self.username,
    #         'email': self.email
    #     }
    

VonUser.set_userDB(vonuserdb, vonusercollection)



    

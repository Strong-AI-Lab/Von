from bson.objectid import ObjectId
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

        # put self in 湛 Zhan von Neumarkt <vonneumarkt@gmail.com>
        user_collection.insert_one({'username': 'zhan', 'email': 'vonneumarkt@gmail.com','password': 'password'})

        cls.userdb = user_database
        cls.usercollection = user_collection
        print(f"VonUser: DB={cls.userdb.name}, Coll={cls.usercollection.name}")

    @classmethod
    def get_userCollection(cls):
        return cls.usercollection
    
    @classmethod
    def get_userDB(cls):
        return cls.userdb



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



    

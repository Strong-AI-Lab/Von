# This is a driver or facade for accessing the database layer from Von
# For now it is implemented in MongoDB but the method names are agnostic
#
# This file requires an installation of MongoDB

import sys, os
import json
from pymongo import MongoClient, errors
from bson.objectid import ObjectId

def get_local_client():

    #print("Entering get_local_client")

    try:
        client = MongoClient('mongodb://localhost:27017/')
        return client
    except errors.ConnectionError as e:
        raise RuntimeError(f"Error connecting to MongoDB: {e}")

class DatabaseDriver:

    client = None

    def __new__(cls, *args, **kwargs):
        cls.client = get_local_client()
        if cls.client is None:
            return None
        return super(DatabaseDriver, cls).__new__(cls)

    def __init__(self):

        #print("Initializing DatabaseDriver")
        return

    def insert_row(self, table, fields):
        ''' 
            inserts a row (item) into a table (collection)
            The item is made up of fields The method will not check that
            no row (item) exists with the provided fields before
            trying to insert them since this does not make sense with NoSQL
            databases. Be careful to be specific with the fields provided.            
        '''

        #print("Entering insert_row")

        if table is None:
            return None
        coll = table

        try:

            keysfilter = {}
            for key in fields.keys():
                keysfilter[key] = {'$exists': True}

            #row = coll.find_one(keysfilter)
            #if row is None:
            result = coll.insert_one(fields)
            #print("Row created successfully.")
            return result.inserted_id
            #else:
            #    #print("Row already exists")
            #    return None
        except errors.PyMongoError as e:
            print(f"Error creating row: {e}")
            return None

    def read_row(self, table, row_id=None, fields=None):
        ''' 
            read a row (item) from a table (collection)
            the method either uses the row_id or a set of field
            values to perform the match.
            
        '''

        #print("Entering read_row")

        if table is None:
            return None
        coll = table
        try:

            if row_id is not None:
                row = coll.find_one({'_id': ObjectId(row_id)})
                if row:
                    return row
                else:
                    #print("Row not found.")
                    return None
            elif fields is not None:
                row = coll.find_one(fields)
                if row:
                    return row
                else:
                    #print("Row not found.")
                    return None
            else:
                return None

        except errors.PyMongoError as e:
            print(f"Error reading row: {e}")
            return None

    def update_row(self, table, row_id, fields):
        ''' 
            updates a row (item) in a table (collection) with a new value
            
        '''

        #print("Entering update_row")

        if table is None:
            return None

        if (fields is None or len(fields) == 0):
            return None

        coll = table
        try:
            result = coll.update_one(
                {'_id': ObjectId(row_id)},
                {'$set': fields}
            )
            if result.matched_count > 0:
                #print("Row updated successfully.")
                return result.upserted_id
            else:
                #print("Row not found.")
                return None
            return result.upserted_id
        except errors.PyMongoError as e:
            print(f"Error updating row: {e}")

        return None

    def delete_row(self, table, row_id):
        ''' 
            deletes a row (item) from a table (collection)
            
        '''

        #print("Entering delete row")

        if table is None:
            return None
        
        coll = table
        try:
            result = coll.delete_one({'_id': ObjectId(row_id)})
            if result.deleted_count > 0:
                #print("Row deleted successfully.")
                return result.deleted_count
            else:
                #print("Row not found.")
                return 0
        except errors.PyMongoError as e:
            print(f"Error deleting row: {e}")
            return None

    def get_database(self, database_name):
        ''' 
            Gets the MongoDB database if it exists
        '''

        #print("Entering get_database")

        try:

            if self.client is not None:

                dblist = self.client.list_database_names()

                if database_name in dblist:
                    db = self.client[database_name]
                    return db
                else:
                    return None
            
            else:
                return None

        except errors.PyMongoError as e:
            print(f"Error connecting to MongoDB: {e}")
            return None

    def create_database(self, database_name):
        ''' 
            Creates a database (in MongoDB)
            
        '''

        #print("Entering create_database")

        try:
            if self.client is not None:
                dblist = self.client.list_database_names()

                if database_name not in dblist:
                    db = self.client[database_name] # This will create a new database only if data is stored in it. 
                    return db
                else:
                    return None
            else:
                return None

        except errors.PyMongoError as e:
            return None

    def delete_database(self, database_name):
        ''' 
            Deletes a database (in MongoDB)
            
        '''

        #print("Entering delete_database")

        try:
            if self.client is not None:
                self.client.drop_database(database_name)
                return True
            else:
                return None

        except errors.PyMongoError as e:
            return None

    def get_table(self, db, table_name):
        ''' 
            Gets the MongoDB database if it exists
        '''

        #print("Entering get_table")

        try:

            if db is not None:

                tablelist = db.list_collection_names()

                if table_name in tablelist:
                    table = db[table_name]
                    return table
                else:
                    return None
            
            else:
                return None

        except errors.PyMongoError as e:
            print(f"Error connecting to MongoDB: {e}")
            return None

    def create_table(self, db, table_name):
        ''' 
            Creates a table (collection)
            
        '''

        #print("Entering create_table")

        try:

            if db is not None:

                tablelist = db.list_collection_names()

                if table_name not in tablelist:
                    table = db[table_name]
                    return table
                else:
                    return None
            
            else:
                return None

        except errors.PyMongoError as e:
            print(f"Error connecting to MongoDB: {e}")
            return None


        return None

    def read_table(self, db, table_name):
        ''' 
            Reads a table (collection)
        Returns a list of all the rows (items) in the table
            
        '''

        #print("Entering read_table")

        try:

            if db is not None:

                table = db[table_name]

                documents = table.find()

                rows = []
                for document in documents:
                    row = {}
                    for field, value in document.items():
                        row[field] = value
                    rows.append(row)

                return rows

            else:
                return []

        except errors.PyMongoError as e:
                return []
                    
        return []

    def update_table(self, db, table_name, rows, key_name):
        ''' 
            Updates a table (collection) with new rows (items)
            This method is not currently implemented.
            
        '''

        #print("Entering update_table")
        print("This method is not implemented")

        try:

            if db is not None:
                return []
            else:
                return []

        except errors.PyMongoError as e:
            return []
                    
        return []

    def delete_table(self, db, table_name):
        ''' 
            Deletes a table (collection)
            
        '''

        #print("Entering delete_table")

        try:

            if db is not None:

                db[table_name].drop()

                return True
            
            else:
                return None

        except errors.PyMongoError as e:
            print(f"Error connecting to MongoDB: {e}")
            return None


        return None

# Example usage
if __name__ == "__main__":
    # Create a new table_name
    db_name = "ExampleDatabase"
    coll_name = "ExampleTable"

    driver = DatabaseDriver()

    if driver is None:
        print("Invalid driver found or there is a problem with your database system")
        sys.exit(1)

    db = driver.get_database(db_name)
    if db is None:
        db = driver.create_database(db_name)
    print(f"Acquired or created database {db_name}")

    table = driver.get_table(db, coll_name)
    if table is None:
        table = driver.create_table(db, coll_name)
    print(f"Acquired or created table {coll_name} in database {db_name}")

    fields = {} 
    fields["example_key"] = 100
    row_id = driver.insert_row(table, fields)
    print(f'Inserted Row for database_name {db_name} and table_name {coll_name}: {row_id}')

    # Read table_name information
    row = driver.read_row(table, row_id=row_id)  # Replace with actual ObjectId string
    print(f'Read Row for database_name {db_name} and table_name {coll_name}: {row}')
    
    # Update table_name information
    fields = {}
    fields["example_key"] = 200    
    row_id = driver.update_row(table, row["_id"], fields)  # Replace with actual ObjectId string
    
    # Read updated table_name information
    row = driver.read_row(table, fields=fields)  # Replace with actual ObjectId string
    print(f'Updated Row for database_name {db_name} and table_name {coll_name}: {row}')
    
    # Delete table_name
    driver.delete_row(table, row["_id"])  # Replace with actual ObjectId string
    
    # Try to read deleted table_name
    row = driver.read_row(table, row_id=row["_id"])  # Replace with actual ObjectId string
    print(f'Deleted Row for database_name {db_name} and table_name {coll_name}: {row}')

    driver.delete_table(db, coll_name)
    print(f"Deleting table {coll_name} in database {db_name}")

    driver.delete_database(db_name)
    print(f"Deleting database {db_name}")
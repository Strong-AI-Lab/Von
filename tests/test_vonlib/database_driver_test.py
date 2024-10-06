import unittest
from vonlib.database_driver import DatabaseDriver
from pymongo import MongoClient
from bson.objectid import ObjectId

class TestCDatabaseDriver(unittest.TestCase):
    """
    Unit tests for the CDatabaseDriver class in the vonlib.database_driver module.
    This test suite covers both Data Manipulation Language (DML) and Data Definition Language (DDL) methods.
    """

    @classmethod
    def setUpClass(cls):
        """
        Set up the test environment by creating a test database and table.
        This method is called once before all tests.
        """
        cls.driver = DatabaseDriver()        
        cls.db_name = "TestDatabase"
        cls.table_name = "TestTable"

        if cls.driver is None:
            print("setUpClass: Failed to initialize DatabaseDriver")

            cls.skip_all_tests = True # Make sure we know the driver isn't good, and skip tests
            return

        cls.db = cls.driver.create_database(cls.db_name)
        cls.table = cls.driver.create_table(cls.db, cls.table_name)
        cls.skip_all_tests = False

    def setUp(self):

        #print("Entering self.setup")
        if getattr(self, 'skip_all_tests', False):  # If the class setup failed, skip the tests; False is the default value, not the thing to test. This tests whether 'skip_all_tests' has been set to True.
            self.skipTest("DatabaseDriver setup failed in setUpClass")
            return
        if self.driver is None:
            self.skipTest("no valid database system")
            return
        
        result = self.driver.delete_table(self.db, self.table_name)
        if result:
            self.table = self.driver.create_table(self.db, self.table_name)


    @classmethod
    def tearDownClass(cls):
        """
        Clean up the test environment by deleting the test database and table.
        This method is called once after all tests.
        """

        if not getattr(cls, 'skip_all_tests', False):
            cls.driver.delete_database(cls.db_name)
            return

        cls.driver.delete_table(cls.db, cls.table_name)
        cls.driver.delete_database(cls.db_name)

    '''
    def test_create_database(self):
        """
        Test the creation of a new database.
        """
        db_name = "NewTestDatabase"
        db = self.driver.create_database(db_name)
        self.assertIsNotNone(db)
        self.driver.delete_database(db_name)

    def test_delete_database(self):
        """
        Test the deletion of an existing database.
        """
        db_name = "NewTestDatabase"
        self.driver.create_database(db_name)
        result = self.driver.delete_database(db_name)
        self.assertTrue(result)
    '''
    
    def test_create_table(self):
        """
        Test the creation of a new table (collection) in the database.
        """
        table_name = "NewTestTable"
        table = self.driver.create_table(self.db, table_name)
        self.assertIsNotNone(table)
        self.driver.delete_table(self.db, table_name)

    def test_delete_table(self):
        """
        Test the deletion of an existing table (collection) in the database.
        """
        table_name = "NewTestTable"
        self.driver.create_table(self.db, table_name)
        result = self.driver.delete_table(self.db, table_name)
        self.assertTrue(result)

    def test_insert_row(self):
        """
        Test the insertion of a new row (document) into a table (collection).
        """
        fields = {"key": "value1"}
        row_id = self.driver.insert_row(self.table, fields)
        self.assertIsNotNone(row_id)

    def test_read_row_by_id(self):
        """
        Test reading a row (document) from a table (collection) by its ID.
        """
        fields = {"key": "value2"}
        row_id = self.driver.insert_row(self.table, fields)
        #print(f"row id is {row_id}")
        row = self.driver.read_row(self.table, row_id=str(row_id))
        self.assertIsNotNone(row)
        self.assertEqual(row["key"], "value2")

    def test_read_row_by_fields(self):
        """
        Test reading a row (document) from a table (collection) by its fields.
        """
        fields = {"key": "value3"}
        row_id = self.driver.insert_row(self.table, fields)
        row = self.driver.read_row(self.table, fields=fields)
        self.assertIsNotNone(row)
        self.assertEqual(row["key"], "value3")

    def test_update_row(self):
        """
        Test updating an existing row (document) in a table (collection).
        """
        #print(f"the table is now {str(self.table)}")
        fields = {"key": "value4"}
        row_id = self.driver.insert_row(self.table, fields)
        new_fields = {"key": "new_value"}
        self.driver.update_row(self.table, str(row_id), new_fields)
        updated_row = self.driver.read_row(self.table, row_id=str(row_id))
        self.assertEqual(updated_row["key"], "new_value")

    def test_delete_row(self):
        """
        Test deleting a row (document) from a table (collection).
        """
        fields = {"key": "value5"}
        row_id = self.driver.insert_row(self.table, fields)
        delete_count = self.driver.delete_row(self.table, str(row_id))
        self.assertEqual(delete_count, 1)
        deleted_row = self.driver.read_row(self.table, row_id=str(row_id))
        self.assertIsNone(deleted_row)

    def test_read_table(self):
        """
        Test reading all rows (documents) from a table (collection).
        """                
        fields1 = {"key": "value6"}
        fields2 = {"key2": "value7"}
        self.driver.insert_row(self.table, fields1)
        self.driver.insert_row(self.table, fields2)
        rows = self.driver.read_table(self.db, self.table_name)
        self.assertEqual(len(rows), 2)

if __name__ == '__main__':
    unittest.main()

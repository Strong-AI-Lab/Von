import logging
from pymongo.collection import Collection
from pymongo import ASCENDING
from pymongo.errors import OperationFailure

# Imports from mongo_client.py, located in the same directory
from .mongo_client import get_db, APPLICATION_SETTINGS_COLLECTION_NAME

# REFACTORING_NOTE: This file is intended to house MongoDB setup-related functions,
# such as those that ensure collections exist and have the correct indexes.

logger = logging.getLogger(__name__)


def get_application_settings_collection() -> Collection | None:
    """
    Returns the 'application_settings' collection instance from MongoDB.
    Ensures that the necessary index on 'setting_name' exists.

    Returns:
        A PyMongo Collection object for the application settings, or None if an error occurs.
    """
    db = get_db()  # get_db() is imported from mongo_client and handles DB connection
    if db is None:
        logger.error(
            "Could not get database instance (via mongo_client.get_db()). Cannot access application_settings collection."
        )
        return None

    try:
        settings_coll = db[APPLICATION_SETTINGS_COLLECTION_NAME]

        # Ensure index on setting_name for quick lookups and to enforce uniqueness.
        # MongoDB's create_index is idempotent.
        settings_coll.create_index([("setting_name", ASCENDING)], unique=True)
        # print(f"Index on 'setting_name' for collection '{APPLICATION_SETTINGS_COLLECTION_NAME}' ensured.")

        return settings_coll

    except OperationFailure as e:
        logger.warning(
            "MongoDB operation failed while accessing or indexing '%s': %s",
            APPLICATION_SETTINGS_COLLECTION_NAME,
            e,
        )
        return None
    except Exception as e:  # Catch any other unexpected errors
        logger.warning(
            "An unexpected error occurred while getting application settings collection: %s",
            e,
        )
        return None


if __name__ == "__main__":
    # This block allows for direct testing of this script.
    # Note: For this to run, MongoDB must be accessible as configured in mongo_client.py,
    # and the script needs to be run from a context where 'src.backend.db.mongo_client' can be resolved,
    # or the relative import '.mongo_client' works (e.g., run as a module 'python -m src.backend.db.mongo_setup').

    print("--- Testing mongo_setup.py ---")

    print("\nAttempting to retrieve application_settings collection...")
    app_settings_collection = get_application_settings_collection()

    if app_settings_collection is not None:
        print(f"Successfully retrieved collection: '{app_settings_collection.name}'")
        print(f"Database: '{app_settings_collection.database.name}'")

        # Optional: Perform a simple test operation
        # print("\nAttempting a test write and read to the collection...")
        # test_setting_key = "__mongo_setup_test_setting__"
        # try:
        #     app_settings_collection.update_one(
        #         {"setting_name": test_setting_key},
        #         {"$set": {"value": "test_value_from_mongo_setup", "description": "This is a temporary test setting."}},
        #         upsert=True
        #     )
        #     print(f"Upserted test setting: '{test_setting_key}'")

        #     retrieved_setting = app_settings_collection.find_one({"setting_name": test_setting_key})
        #     if retrieved_setting:
        #         print(f"Retrieved test setting: {retrieved_setting}")
        #     else:
        #         print(f"Could not retrieve test setting '{test_setting_key}' after upsert.")

        #     # Clean up the test setting
        #     # app_settings_collection.delete_one({"setting_name": test_setting_key})
        #     # print(f"Cleaned up test setting: '{test_setting_key}'")

        # except Exception as e:
        #     print(f"Error during test write/read operation: {e}")

    else:
        print("Failed to retrieve application_settings collection.")

    print("\n--- mongo_setup.py test complete ---")

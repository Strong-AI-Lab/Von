"""Implement all the CRUD operations for the MongoDB database."""

import datetime
import logging
from typing import Union, List
from bson.objectid import ObjectId
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError
from pymongo.cursor import Cursor

# from pymongo.mongo_client import MongoClient
# from pymongo.write_concern import WriteConcern
# from pymongo.read_concern import ReadConcern

from paper_recommender.paper_extraction.base import Paper

logger = logging.getLogger(__name__)


def get_projects_for_user(db: Database, user_id: str) -> Union[List, bool]:
    """Get all the projects of a user.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.

    Returns:
        The current list of projects of the user.
        False if the user does not exist or an error occurred.
    """
    user = db.users.find_one({"_id": user_id})
    if user:
        try:
            return user["projects"]
        except Exception:
            logger.error(
                "The user '%s' exists, but failed to retrieve projects for the user.",
                user_id,
            )
            return False
    else:
        return False


def keyword_search_papers(db: Database, keywords: str) -> Union[Cursor, bool]:
    """Search papers by keywords. Will tokenize the search string using whitespace and most punctuation as delimiters,
    and perform a logical OR of all such tokens in the search string.

    Args:
        db: The MongoDB database object.
        keywords: The keywords to search for.

    Returns:
        A curser object for papers that contain the keywords in the title or abstract or author list.
        False if an error occurred.
    """
    try:
        papers = db.papers.find(
            {"$text": {"$search": keywords}},
            projection={
                "title": 1,
                "authors": 1,
                "url": 1,
            },
            limit=5,
        )

        return papers
    except Exception:
        logger.exception("Failed to search papers by keywords.")
        return False


def user_exists(db: Database, user_id: str) -> bool:
    """Check if the user exists in the database.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.

    Returns:
        True if the user exists in the database. False otherwise."""
    user = db.users.find_one({"_id": user_id})
    return True if user else False


def create_user(db: Database, user_id: str) -> bool:
    """Create a new user in the database.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.

    Returns:
        True if the operation is successful. False otherwise."""
    try:
        db.users.insert_one(
            {
                "_id": user_id,
                "vip": False,
                "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
            }
        )
        return True
    except DuplicateKeyError:
        logger.error("User '%s' already exists.", user_id)
        return False
    except Exception:
        logger.exception("Failed to create user '%s'.", user_id)
        return False


def update_user_vip_status(db: Database, user_id: str, vip_status: bool) -> bool:
    """Update the VIP status of a user.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.
        vip_status: The VIP status in boolean.

    Returns:
        True if the operation is successful. False otherwise."""
    try:
        response = db.users.update_one({"_id": user_id}, {"$set": {"vip": vip_status}}, upsert=False)
        return response.raw_result["updatedExisting"]  # type: ignore
    except Exception:
        logger.exception("Failed to update the VIP status of user '%s'.", user_id)
        return False


def add_project(db: Database, user_id: str, project_title: str, project_description: str) -> bool:
    """Add a new project for a user.

    NOTE: This function can't guarantee atomicity of the operations.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.
        project_title: The title of the project in string.
        project_description: The description of the project in string.

    Returns:
        True if the operation is successful. False otherwise."""

    try:
        # create a new document in the projects collection
        project_id = db.projects.insert_one(
            {
                "user_id": user_id,
                "title": project_title,
                "description": project_description,
                "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
            }
        ).inserted_id

        # update the user's current_project embedded document
        response = db.users.update_one(
            {"_id": user_id},
            {
                "$push": {
                    "projects": {
                        "title": project_title,
                        "description": project_description,
                        "project_id": project_id,
                    }
                }
            },
            upsert=False,
        )
        return response.raw_result["updatedExisting"]  # type: ignore
    except Exception:
        logger.exception("Failed to add a project for user '%s'.", user_id)
        return False


def delete_project(db: Database, user_id: str, project_id: Union[ObjectId, str]) -> bool:
    """Delete a project for a user.

    NOTE: we do not delete the project document from the projects collection;
        we only remove it from the user's embedded document in the users collection.

    Args:
        db: The MongoDB database object.
        user_id: The user ID in string.
        project_id: The project ID in ObjectId or string.

    Returns:
        True if the operation is successful. False otherwise."""
    if isinstance(project_id, str):
        project_id = ObjectId(project_id)
    try:
        # update the user's current_project embedded document
        response = db.users.update_one(
            {"_id": user_id},
            {
                "$pull": {
                    "projects": {
                        "project_id": project_id,
                    }
                }
            },
            upsert=False,
        )
        return response.raw_result["updatedExisting"]  # type: ignore
    except Exception:
        logger.exception("Failed to delete a project for user '%s'.", user_id)
        return False


# def insert_update_project_transaction(
#     client: MongoClient,
#     db: Database,
#     user_id: str,
#     project_description: str,
# ) -> bool:
#     """Insert or update the project description of the user.

#     NOTE: This function is used to ensure the atomicity of the operations in the transaction.
#     It can't be used in a standalone instance; must use replica set deployment.

#     Args:
#         client: The MongoClient object.
#         db: The MongoDB database object.
#         user_id: The user ID in string.
#         project_description: The description of the project in string.

#     Returns:
#         True if the operation is successful. False otherwise."""

#     # Define the callback that specifies the sequence of operations to perform inside the transactions.
#     def callback(session):
#         # create a new document in the projects collection
#         project_id = db.projects.insert_one(
#             {
#                 "user_id": user_id,
#                 "description": project_description,
#                 "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
#             },
#             session=session,
#         ).inserted_id

#         # update the user's current_project embedded document
#         response = db.users.update_one(
#             {"_id": user_id},
#             {
#                 "$set": {
#                     "current_project": {
#                         "description": project_description,
#                         "project_id": project_id,
#                     }
#                 }
#             },
#             upsert=False,
#             session=session,
#         )
#         return response.raw_result["updatedExisting"]

#     # update the user's embedded document
#     try:
#         # start a transaction, execute the callback, and commit (or abort on error).
#         with client.start_session() as session:
#             return session.with_transaction(
#                 callback,
#                 write_concern=WriteConcern(
#                     "majority", wtimeout=3000
#                 ),  # used in a distributed setting with multiple nodes
#                 read_concern=ReadConcern("local"),  # used in a distributed setting with multiple nodes
#                 # read_preference=ReadPreference.PRIMARY, # used for replication
#             )
#     except Exception:
#         logger.exception("Failed to insert or update the project description for user '%s' in a transaction.", user_id)
#         return False


def insert_paper(db: Database, paper: Paper) -> Union[ObjectId, bool]:
    """Insert a new document to the papers collection.

    args:
        db: The MongoDB database object.
        paper: The Paper object.

    Returns:
        The paper ID (ObjectId) if the operation is successful. False otherwise."""
    try:
        paper_id = db.papers.insert_one(
            {
                "url": paper.url,
                "title": paper.title,
                "authors": paper.authors,
                "abstract": paper.abstract,
                "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
            }
        ).inserted_id
        return paper_id
    except DuplicateKeyError:
        # if the paper already exists, return the existing paper id
        # we still want to return the paper id,
        # because a recommendation may still be created since project might be different
        response = db.papers.find_one({"url": paper.url, "title": paper.title, "authors": paper.authors})
        if response:
            return response["_id"]
        else:
            logger.error(
                (
                    "The paper exists in database when trying to insert, "
                    "but failed to retrieve it from the database. URL: '%s'."
                ),
                paper.url,
            )
            return False
    except Exception:
        logger.exception("Failed to insert a new paper extracted from: %s.", paper.url)
        return False


def insert_recommendation(db: Database, record: dict) -> Union[ObjectId, bool]:
    """Insert a new document to the recommendations collection.

    Args:
        db: The MongoDB database object.
        record: The recommendation record in dictionary.

    Returns:
        The recommendation ID (ObjectId) if the operation is successful. False otherwise."""
    try:
        recommendation_id = db.recommendations.insert_one(record).inserted_id
        return recommendation_id
    # except DuplicateKeyError:
    #     # if the recommendation already exists, return False
    #     return False
    except Exception:
        logger.exception("Failed to insert a new recommendation for user '%s'.", record["user_id"])
        return False


def update_recommendation_feedback(
    db: Database,
    recommendation_id: Union[ObjectId, str],
    agreement: bool,
) -> bool:
    """Update the recommendation with user feedback on the decision.

    Args:
        db: The MongoDB database object.
        recommendation_id: The recommendation ID in ObjectId or string.
        agreement: The user agreement in boolean.

    Returns:
        True if the operation is successful. False otherwise."""
    if isinstance(recommendation_id, str):
        recommendation_id = ObjectId(recommendation_id)
    try:
        response = db.recommendations.update_one(
            {"_id": recommendation_id},
            {"$set": {"feedback.agreement": agreement}},
            upsert=False,
        )
        return response.raw_result["updatedExisting"]  # type: ignore
    except Exception:
        logger.exception("Failed to update the recommendation '%s' with user agreement feedback.", recommendation_id)
        return False


def update_recommendation_feedback_reason(
    db: Database,
    recommendation_id: Union[ObjectId, str],
    reason: str,
) -> bool:
    """Update the recommendation with user's explanation/reason for the feedback.

    Args:
        db: The MongoDB database object.
        recommendation_id: The recommendation ID in ObjectId or string.
        reason: The user's reason in string.

    Returns:
        True if the operation is successful. False otherwise."""
    if isinstance(recommendation_id, str):
        recommendation_id = ObjectId(recommendation_id)
    try:
        response = db.recommendations.update_one(
            {"_id": recommendation_id},
            {"$set": {"feedback.reason": reason}},
            upsert=False,
        )
        return response.raw_result["updatedExisting"]  # type: ignore
    except Exception:
        logger.exception(
            "Failed to update the recommendation '%s' with user's explanation of the feeback.", recommendation_id
        )
        return False

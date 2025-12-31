import os
import sys
import logging
from pathlib import Path
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, OperationFailure
import datetime  # Added for type hinting and __main__ example

logger = logging.getLogger(__name__)


def _debug_mongo_enabled() -> bool:
    return os.environ.get("VON_DEBUG_MONGO", "").lower() in {"1", "true", "yes", "on"}


def _redact_mongo_uri_for_log(uri: str) -> str:
    redacted = uri
    if "@" in redacted:
        prefix, rest = redacted.split("://", 1)
        if "@" in rest:
            creds, hostpart = rest.split("@", 1)
            user = creds.split(":", 1)[0] if ":" in creds else creds
            redacted = f"{prefix}://{user}:***@{hostpart}"
    return redacted


def _host_display_from_uri(uri: str) -> str:
    # Best-effort host display, safe for logs.
    redacted = _redact_mongo_uri_for_log(uri)
    if "@" in redacted:
        return redacted.split("@")[-1].split("/")[0]
    if "://" in redacted:
        return redacted.split("://", 1)[1].split("/")[0]
    return redacted.split("/")[0]


USE_MOCK_DB = os.environ.get("VON_USE_MOCK_DB", "0").lower() in {"1", "true", "yes"}
mongomock = None
if USE_MOCK_DB:
    try:  # pragma: no cover - optional dependency import
        import mongomock as _mongomock

        mongomock = _mongomock
    except Exception as exc:  # pragma: no cover
        mongomock = None
        logger.warning("VON_USE_MOCK_DB set but mongomock import failed: %s", exc)

# Import colorama for colored console output
try:
    from colorama import Fore, Style, init

    # Initialize colorama for Windows compatibility
    init()
    COLORAMA_AVAILABLE = True
except ImportError:
    # Fallback if colorama is not installed
    COLORAMA_AVAILABLE = False

    class MockColor:
        BLUE = ""
        RESET_ALL = ""

    Fore = MockColor()
    Style = MockColor()

# REFACTORING_NOTE: This file is being updated to support a generalized entity model.
# The goal is to move away from a \'people\'-specific model to one that can handle
# various types of entities (types from vontology, and individuals of those types stored in DB).
# This involves adding new collections and accessors for \'entities\' and \'user_entity_tracking\'.

# --- Configuration ---
# Load environment variables from a .env file (if present) before reading MONGO_URI.
# This allows running the app without exporting env vars in the shell.
try:
    # Lazy import to avoid hard dependency if not installed
    from dotenv import load_dotenv, find_dotenv  # type: ignore

    # Prefer an explicit repo-root .env if available; otherwise rely on find_dotenv()
    _this_file = Path(__file__).resolve()
    _repo_root = (
        _this_file.parents[3] if len(_this_file.parents) >= 4 else _this_file.parent
    )
    _explicit_env = _repo_root / ".env"
    if _explicit_env.exists():
        load_dotenv(_explicit_env, override=False)
    else:
        load_dotenv(find_dotenv(), override=False)
except Exception:
    # Safe no-op if python-dotenv isn't installed or .env isn't found
    pass

# Use environment variables or default to localhost
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
# Optional fallback URI to use when SRV DNS resolution fails (useful offline)
MONGO_LOCAL_URI = os.environ.get(
    "MONGO_LOCAL_URI", "mongodb://127.0.0.1:27017/?directConnection=true"
)
MONGO_ALLOW_LOCAL_FALLBACK = os.environ.get("MONGO_ALLOW_LOCAL_FALLBACK", "1") in (
    "1",
    "true",
    "True",
)


def _is_running_under_pytest() -> bool:
    # PYTEST_CURRENT_TEST is the most reliable indicator.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def get_configured_database_name() -> str:
    """Return the currently configured Mongo database name.

    This reads the environment at call-time (not import-time).
    """

    name = os.environ.get("VON_DB_NAME")
    if not isinstance(name, str) or not name.strip():
        return "von_db"
    return name.strip()


# DEPRECATED: Prefer get_configured_database_name() which reads the environment at
# call-time. This module-level constant is retained for compatibility.
DATABASE_NAME = get_configured_database_name()


def assert_safe_database_name_for_pytest(db_name: str) -> None:
    """Fail fast if pytest is pointed at the production DB name."""

    if not _is_running_under_pytest():
        return
    if db_name == "von_db":
        raise RuntimeError(
            "Safety guard: pytest is configured to use VON_DB_NAME=von_db. "
            "Refusing to connect because tests may delete collections. "
            "Set VON_DB_NAME=test_von_db (recommended) before running pytest."
        )


def assert_destructive_db_operation_allowed(operation: str) -> None:
    """Block destructive DB operations on the production DB by default.

    Set VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS=1 to override.
    """

    db_name = get_configured_database_name()
    if db_name != "von_db":
        return
    allow = os.environ.get("VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow:
        raise RuntimeError(
            f"Safety guard: refusing destructive DB operation '{operation}' on VON_DB_NAME=von_db. "
            "Set VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS=1 to override (dangerous)."
        )


# REFACTORING_NOTE: Collection names defined as per the refactoring plan.
PEOPLE_COLLECTION_NAME = "people"  # To be phased out.
ENTITIES_COLLECTION_NAME = "entities"  # DEPRECATED: Use CONCEPTS_COLLECTION_NAME
CONCEPTS_COLLECTION_NAME = "concepts"
USER_ENTITY_TRACKING_COLLECTION_NAME = "user_entity_tracking"
INTERACTIONS_COLLECTION_NAME = "interactions"  # Existing, but schema will be updated.
INTERACTION_LOG_COLLECTION_NAME = "interaction_log"  # Existing, for auditing.
MIGRATIONS_LOG_COLLECTION_NAME = (
    "migrations_log"  # New collection for tracking migrations
)
APPLICATION_SETTINGS_COLLECTION_NAME = (
    "application_settings"  # New collection for application-wide settings
)
TEXT_VALUES_COLLECTION_NAME = (
    "text_values"  # New collection for language-tagged text values
)
TEXT_RELATIONS_COLLECTION_NAME = (
    "text_relations"  # New collection linking concepts to text values
)
META_RELATIONS_COLLECTION_NAME = "meta_relations"  # New collection for relation elicitation meta-data (JVNAUTOSCI-371)

# Agent Gmail OAuth token storage (JVNAUTOSCI-801)
AGENT_GMAIL_TOKENS_COLLECTION_NAME = "agent_gmail_tokens"

# Room device identity (JVNAUTOSCI-884)
ROOM_DEVICES_COLLECTION_NAME = "room_devices"

# --- Client Initialization ---
# REFACTORING_NOTE: The global client is being removed in favor of a more robust
# connection management pattern within get_db(). This avoids issues with
# initialization order and makes the connection more resilient.
_mongo_client = None
# Track whether we are using a fallback URI (local) rather than the primary MONGO_URI
_using_fallback = False
_effective_uri = MONGO_URI  # The URI actually used to create the client


def get_db() -> Database | None:
    """
    Establishes a connection to the MongoDB database if one doesn't exist,
    and returns the database instance.
    """
    global _mongo_client
    global _using_fallback, _effective_uri
    db_name = get_configured_database_name()
    assert_safe_database_name_for_pytest(db_name)

    if USE_MOCK_DB:
        if _mongo_client is None:
            if mongomock is None:
                raise RuntimeError(
                    "VON_USE_MOCK_DB is enabled but mongomock is unavailable"
                )
            _mongo_client = mongomock.MongoClient()
            _effective_uri = "mongomock://"
            _using_fallback = False
        return _mongo_client[db_name]

    if _mongo_client is None:
        try:
            # Try without SSL/TLS as a last resort (INSECURE but may work for development)
            _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            # The ismaster command is cheap and does not require auth.
            _mongo_client.admin.command("ismaster")
            _effective_uri = MONGO_URI
            _using_fallback = False
            if _debug_mongo_enabled():
                try:
                    logger.debug(
                        "[MongoConnect] Connected primary URI host(s): %s",
                        _host_display_from_uri(MONGO_URI),
                    )
                except Exception:
                    # Never allow debug logging failures to affect connection behaviour.
                    pass
        except ConnectionFailure as e:
            logger.warning("Error connecting to MongoDB (ConnectionFailure): %s", e)
            if "SSL" in str(e) and "TLSV1_ALERT_INTERNAL_ERROR" in str(e):
                logger.warning(
                    "[VON_ASSISTANT_SUGGESTION] This appears to be an SSL/TLS handshake error. "
                    "This commonly occurs when the current IP address is not whitelisted in MongoDB Atlas. "
                    "IMPORTANT: IP whitelists are per-project, not per-organisation. "
                    "Verify your IP is whitelisted in the correct project: '%s'. "
                    "Steps: Atlas Console → Select correct Organisation → Select correct Project → Security → Network Access."
                    " Your IP should be listed as Active in the project that contains your cluster.",
                    os.environ.get("MONGO_PROJECT", "Unknown"),
                )

            _mongo_client = None  # Ensure client is None on failure

            # Check for common SSL/Connection errors that warrant a fallback attempt
            # WinError 10054: Connection reset by peer (common with IP whitelist blocks)
            # SSL handshake failed: Generic SSL failure
            is_ssl_error = (
                "SSL" in str(e) or "10054" in str(e) or "handshake" in str(e).lower()
            )

            # Debug logging for fallback logic
            if _debug_mongo_enabled():
                logger.debug(
                    "[MongoConnect] Connection failed. is_ssl_error=%s", is_ssl_error
                )
                logger.debug(
                    "[MongoConnect] MONGO_ALLOW_LOCAL_FALLBACK=%s",
                    MONGO_ALLOW_LOCAL_FALLBACK,
                )
                logger.debug(
                    "[MongoConnect] MONGO_URI starts with mongodb+srv://: %s",
                    MONGO_URI.startswith("mongodb+srv://"),
                )

            # Attempt local fallback for SRV/DNS issues OR SSL blocks if allowed
            # We relax the srv check if explicit fallback is enabled and we have an SSL error
            should_try_fallback = MONGO_ALLOW_LOCAL_FALLBACK and (
                MONGO_URI.strip("\"'").startswith("mongodb+srv://") or is_ssl_error
            )

            if should_try_fallback:
                try:
                    logger.warning(
                        "[mongo_fallback] Attempting local MongoDB fallback due to connection failure."
                    )
                    _mongo_client = MongoClient(
                        MONGO_LOCAL_URI, serverSelectionTimeoutMS=3000
                    )
                    _mongo_client.admin.command("ismaster")
                    _effective_uri = MONGO_LOCAL_URI
                    _using_fallback = True
                    try:
                        logger.warning(
                            "[mongo_fallback] Using local fallback Mongo URI instead of primary (connection failure)."
                        )
                    except Exception:
                        pass
                    if _debug_mongo_enabled():
                        try:
                            logger.debug(
                                "[MongoConnect] Local fallback host: %s",
                                _host_display_from_uri(MONGO_LOCAL_URI),
                            )
                        except Exception:
                            pass
                except Exception as fe:
                    logger.warning("Local fallback connection failed: %s", fe)
                    _mongo_client = None
                    return None
            else:
                return None
        except Exception as e:
            logger.warning(
                "An unexpected error occurred during MongoDB client initialisation: %s",
                e,
            )
            # Attempt local fallback for DNS resolution errors (common with SRV)
            if MONGO_ALLOW_LOCAL_FALLBACK and (
                "resolution" in str(e).lower()
                or "dns" in str(e).lower()
                or MONGO_URI.startswith("mongodb+srv://")
            ):
                try:
                    logger.warning(
                        "[mongo_fallback] Attempting local MongoDB fallback due to DNS/SRV error."
                    )
                    _mongo_client = MongoClient(
                        MONGO_LOCAL_URI, serverSelectionTimeoutMS=3000
                    )
                    _mongo_client.admin.command("ismaster")
                    _effective_uri = MONGO_LOCAL_URI
                    _using_fallback = True
                    try:
                        logger.warning(
                            "[mongo_fallback] Using local fallback Mongo URI instead of primary (DNS/SRV error)."
                        )
                    except Exception:
                        pass
                    if _debug_mongo_enabled():
                        try:
                            logger.debug(
                                "[MongoConnect] Local fallback host: %s",
                                _host_display_from_uri(MONGO_LOCAL_URI),
                            )
                        except Exception:
                            pass
                except Exception as fe:
                    logger.warning("Local fallback connection failed: %s", fe)
                    _mongo_client = None
                    return None
            else:
                _mongo_client = None  # Ensure client is None on failure
                return None

        if _mongo_client:
            print_connection_info()

    if _mongo_client:
        return _mongo_client[db_name]
    return None


def print_connection_info():
    """Log helpful connection info on startup."""
    global _effective_uri
    uri_to_use = _effective_uri if _effective_uri else MONGO_URI
    host = _host_display_from_uri(uri_to_use)
    if "localhost" in host or "127.0.0.1" in host:
        logger.info("[Von Database] Connecting to LOCAL MongoDB at %s", host)
    else:
        logger.info("[Von Database] Connecting to REMOTE MongoDB at %s", host)


def get_effective_mongo_uri() -> str:
    """Return the URI that was effectively used to create the client (may be fallback)."""
    return _effective_uri


def is_using_fallback_uri() -> bool:
    """Return True if a local fallback URI is being used instead of the primary MONGO_URI."""
    return _using_fallback


def close_connection():
    """Closes the MongoDB connection."""
    global _mongo_client
    if _mongo_client:
        _mongo_client.close()
        _mongo_client = None


def test_connection(verbose: bool = False) -> bool:
    """Test MongoDB connection and return True if successful, False otherwise."""
    db = get_db()
    if db is None:
        if verbose:
            logger.warning("MongoDB client is not initialised or connection failed.")
        return False

    try:
        # Test the connection with a simple ping
        db.command("ping")
        if verbose:
            logger.info("MongoDB connection successful.")
        return True
    except ConnectionFailure as e:
        if verbose:
            logger.warning("MongoDB connection failed (ConnectionFailure): %s", e)
        return False
    except Exception as e:
        if verbose:
            logger.warning("MongoDB connection test failed: %s", e)
        return False


# --- Collection Access ---
def get_people_collection() -> Collection | None:
    """DEPRECATED: Returns the 'people' collection instance. Will be replaced by get_entities_collection.
    REFACTORING_NOTE: This accessor is deprecated and will be removed after data migration
    and all calling code is updated to use the new entity services.
    """
    db = get_db()
    if db is not None:  # Changed from 'if db:'
        return db[PEOPLE_COLLECTION_NAME]
    return None


def get_interactions_collection() -> Collection | None:
    """Returns the 'interactions' collection instance."""
    db = get_db()
    if db is not None:  # Changed from 'if db:'
        return db[INTERACTIONS_COLLECTION_NAME]
    return None


def get_interaction_log_collection() -> Collection | None:
    """Returns the 'interaction_log' collection instance."""
    db = get_db()
    if db is not None:
        return db[INTERACTION_LOG_COLLECTION_NAME]
    return None


# REFACTORING_NOTE: New accessor for the generalized \'entities\' collection.
# This will be used by new entity services for CRUD operations on individuals.


def get_concepts_collection() -> Collection | None:
    """Returns the unified 'concepts' collection instance.
    This collection contains all concepts from the previously separate
    vontology_nodes and entities collections.
    """
    db = get_db()
    if db is not None:
        concepts_coll = db[CONCEPTS_COLLECTION_NAME]
        # Ensure indexes for unified concepts collection (idempotent)
        try:
            # Check if indexes already exist to avoid conflicts
            existing_indexes = [idx["name"] for idx in concepts_coll.list_indexes()]

            # Index for concept_id (primary identifier)
            # Check for both old and new index names to avoid conflicts
            if (
                "concept_id_1_unique" not in existing_indexes
                and "concept_id_unique" not in existing_indexes
            ):
                concepts_coll.create_index(
                    [("concept_id", ASCENDING)], unique=True, name="concept_id_1_unique"
                )

            # Indexes for relationship fields (Phase 2 clear naming)
            if "relationships.is_a_type_of_1" not in existing_indexes:
                concepts_coll.create_index([("relationships.is_a_type_of", ASCENDING)])
            if "relationships.has_subtype_1" not in existing_indexes:
                concepts_coll.create_index([("relationships.has_subtype", ASCENDING)])
            if "relationships.is_an_instance_of_1" not in existing_indexes:
                concepts_coll.create_index(
                    [("relationships.is_an_instance_of", ASCENDING)]
                )
            if "relationships.has_instance_1" not in existing_indexes:
                concepts_coll.create_index([("relationships.has_instance", ASCENDING)])
            if "relationships.related_to_1" not in existing_indexes:
                concepts_coll.create_index([("relationships.related_to", ASCENDING)])

            # Metadata/name indexes
            if "metadata.concept_type_1" not in existing_indexes:
                concepts_coll.create_index([("metadata.concept_type", ASCENDING)])
            # Migrate away from metadata.title text index to name text index
            if "name_text" not in existing_indexes:
                try:
                    concepts_coll.create_index(
                        [("name", "text")], name="name_text", default_language="none"
                    )
                except Exception as _e:
                    # Best-effort; some MongoDB configurations may restrict text indexes
                    logger.warning(f"Unable to create text index on name: {_e}")
            # New: Support searching within names[] (canonical storage for display names)
            if "names.name_1" not in existing_indexes:
                concepts_coll.create_index(
                    [("names.name", ASCENDING)], name="names.name_1"
                )
            # Legacy compatibility: some names[] entries might use 'text' instead of 'name'
            if "names.text_1" not in existing_indexes:
                concepts_coll.create_index(
                    [("names.text", ASCENDING)], name="names.text_1"
                )

            # Timestamp indexes
            if "timestamps.created_at_-1" not in existing_indexes:
                concepts_coll.create_index([("timestamps.created_at", DESCENDING)])
            if "timestamps.updated_at_-1" not in existing_indexes:
                concepts_coll.create_index([("timestamps.updated_at", DESCENDING)])

        except OperationFailure as e:
            logger.warning(
                f"Could not create some indexes for concepts collection: {e}"
            )
        except Exception as e:
            logger.warning(f"Index creation skipped for concepts collection: {e}")

        return concepts_coll
    return None


def get_entities_collection() -> Collection | None:
    """Returns the \'entities\' collection instance.
    REFACTORING_NOTE: Ensures indexes as per the design in concept_refactoring.md.
    """
    db = get_db()
    if db is not None:
        entities_coll = db[ENTITIES_COLLECTION_NAME]
        # REFACTORING_NOTE: Create indexes as defined in concept_refactoring.md (Sub-Task 1.2)
        # This is idempotent; MongoDB creates them only if they don\'t exist or have changed.
        try:
            entities_coll.create_index([("vontology_path", ASCENDING)])
            entities_coll.create_index(
                [
                    ("name", "text"),
                    ("description", "text"),
                    # ("attributes.$**", "text") # Index all values within the attributes object - REMOVED TO PREVENT WARNING
                ],
                name="entities_text_search_index",
                default_language="none",
            )  # Added default_language='none' for broader compatibility
            entities_coll.create_index([("system_tags", ASCENDING)])
            entities_coll.create_index([("user_tags", ASCENDING)])
            entities_coll.create_index(
                [("linked_entities.target_entity_id", ASCENDING)]
            )
            # print(f"Indexes for \'{ENTITIES_COLLECTION_NAME}\' ensured.")
        except OperationFailure as e:
            logger.warning(
                "Error creating indexes for '%s': %s. This might happen with certain MongoDB configurations (e.g., free tier Atlas).",
                ENTITIES_COLLECTION_NAME,
                e,
            )
        except Exception as e:
            logger.warning(
                "An unexpected error occurred during index creation for '%s': %s",
                ENTITIES_COLLECTION_NAME,
                e,
            )
        return entities_coll
    return None


# REFACTORING_NOTE: New accessor for the \'user_entity_tracking\' collection.
# This will be used to manage recent and key entities for the UI, and to track entity usage.
def get_user_entity_tracking_collection() -> Collection | None:
    """Returns the \'user_entity_tracking\' collection instance.
    REFACTORING_NOTE: Ensures indexes as per the design in concept_refactoring.md.
    """
    db = get_db()
    if db is not None:
        user_tracking_coll = db[USER_ENTITY_TRACKING_COLLECTION_NAME]
        # REFACTORING_NOTE: Create indexes as defined in concept_refactoring.md (Sub-Task 1.2)
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("entity_id", ASCENDING)], unique=True)
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("last_accessed_at", DESCENDING)])
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("is_key_entity", ASCENDING)])
        # Actual indexes to be confirmed based on query patterns. For now, ensuring collection access.
        # For Sub-Task 5.4 (concept_refactoring.md), no specific indexes were detailed for this collection,
        # but common ones would be on user_id and last_accessed_at or is_key_entity.
        # For now, let's assume a general index on user_id might be useful.
        user_tracking_coll.create_index([("user_id", ASCENDING)])
        return user_tracking_coll
    return None


# REFACTORING_NOTE: New accessor for the 'migrations_log' collection.
# This will be used to track the status of data migrations.
def get_migrations_log_collection() -> Collection | None:
    """Returns the 'migrations_log' collection instance.
    Ensures indexes for efficient querying of migration status.
    """
    db = get_db()
    if db is not None:
        migrations_log_coll = db[MIGRATIONS_LOG_COLLECTION_NAME]
        # Index on migration_name for quick lookups, should be unique.
        migrations_log_coll.create_index([("migration_name", ASCENDING)], unique=True)
        # Index on completed_at for sorting or querying by time.
        migrations_log_coll.create_index([("completed_at", DESCENDING)])
        return migrations_log_coll
    return None


def get_application_settings_collection() -> Collection | None:
    """Returns the 'application_settings' collection instance.
    Ensures indexes for efficient querying of settings.
    """
    db = get_db()
    if db is not None:
        settings_coll = db[APPLICATION_SETTINGS_COLLECTION_NAME]
        # Index on setting_name for quick lookups, should be unique if setting_name is the primary key.
        # If multiple documents can have the same setting_name (e.g. for different users, though not the case here),
        # then unique=True should be omitted or a compound index used.
        # For global settings, setting_name should be unique.
        try:
            settings_coll.create_index([("setting_name", ASCENDING)], unique=True)
            # print(f"Indexes for '{APPLICATION_SETTINGS_COLLECTION_NAME}' ensured.")
        except OperationFailure as e:
            logger.warning(
                "Error creating indexes for '%s': %s",
                APPLICATION_SETTINGS_COLLECTION_NAME,
                e,
            )
        except Exception as e:
            logger.warning(
                "An unexpected error occurred during index creation for '%s': %s",
                APPLICATION_SETTINGS_COLLECTION_NAME,
                e,
            )
        return settings_coll
    return None


# New accessors for text value storage (JVNAUTOSCI-333)
def get_text_values_collection() -> Collection | None:
    """Returns the 'text_values' collection instance and ensures indexes.
    Stores language-tagged text values referenced by relations.
    """
    db = get_db()
    if db is not None:
        coll = db[TEXT_VALUES_COLLECTION_NAME]
        try:
            existing_indexes = [idx["name"] for idx in coll.list_indexes()]

            # Full-text search on text content
            if "text_text_search" not in existing_indexes:
                coll.create_index([("text", "text")], name="text_text_search")

            # Language code for filtering
            if "lang_1" not in existing_indexes:
                coll.create_index([("lang", ASCENDING)])

            # Optional fingerprint + lang unique combo for dedup (only when fingerprint exists)
            if "fingerprint_lang_unique" not in existing_indexes:
                coll.create_index(
                    [("fingerprint", ASCENDING), ("lang", ASCENDING)],
                    name="fingerprint_lang_unique",
                    unique=True,
                    partialFilterExpression={
                        "fingerprint": {"$exists": True, "$type": "string"}
                    },
                )

            # Timestamps
            if "created_at_-1" not in existing_indexes:
                coll.create_index([("created_at", DESCENDING)], name="created_at_-1")
            if "updated_at_-1" not in existing_indexes:
                coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
        except OperationFailure as e:
            logger.warning(
                f"Could not create some indexes for {TEXT_VALUES_COLLECTION_NAME}: {e}"
            )
        except Exception as e:
            logger.warning(
                f"Index creation skipped for {TEXT_VALUES_COLLECTION_NAME}: {e}"
            )
        return coll
    return None


def get_text_relations_collection() -> Collection | None:
    """Returns the 'text_relations' collection instance and ensures indexes.
    Stores links between concept documents and text_values by predicate.
    """
    db = get_db()
    if db is not None:
        coll = db[TEXT_RELATIONS_COLLECTION_NAME]
        try:
            existing_indexes = [idx["name"] for idx in coll.list_indexes()]

            # Fast lookups by subject, predicate, and object
            if "subject_concept_id_1" not in existing_indexes:
                coll.create_index(
                    [("subject_concept_id", ASCENDING)], name="subject_concept_id_1"
                )
            if "predicate_1" not in existing_indexes:
                coll.create_index([("predicate", ASCENDING)], name="predicate_1")
            if "object_text_id_1" not in existing_indexes:
                coll.create_index(
                    [("object_text_id", ASCENDING)], name="object_text_id_1"
                )

            # Enforce uniqueness of the same triple
            if "subject_predicate_object_unique" not in existing_indexes:
                coll.create_index(
                    [
                        ("subject_concept_id", ASCENDING),
                        ("predicate", ASCENDING),
                        ("object_text_id", ASCENDING),
                    ],
                    name="subject_predicate_object_unique",
                    unique=True,
                )

            # Timestamps
            if "created_at_-1" not in existing_indexes:
                coll.create_index([("created_at", DESCENDING)], name="created_at_-1")
            if "updated_at_-1" not in existing_indexes:
                coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
        except OperationFailure as e:
            logger.warning(
                f"Could not create some indexes for {TEXT_RELATIONS_COLLECTION_NAME}: {e}"
            )
        except Exception as e:
            logger.warning(
                f"Index creation skipped for {TEXT_RELATIONS_COLLECTION_NAME}: {e}"
            )
        return coll
    return None


def get_meta_relations_collection() -> Collection | None:
    """Returns the 'meta_relations' collection instance and ensures indexes.
    Stores auxiliary mapping documents for relation elicitation:
      - suggested_relations_for_type
      - user_question_prompt_for_relation
      - understand_user_response_for_relation
    Index strategy:
      - type + subject_type_id (for suggested lists)
      - type + predicate_id (for prompt templates)
      - updated_at descending (optional admin queries)
    """
    db = get_db()
    if db is not None:
        coll = db[META_RELATIONS_COLLECTION_NAME]
        try:
            existing_indexes = [idx["name"] for idx in coll.list_indexes()]
            if "type_1_subject_type_id_1" not in existing_indexes:
                coll.create_index(
                    [("type", ASCENDING), ("subject_type_id", ASCENDING)],
                    name="type_1_subject_type_id_1",
                )
            if "type_1_predicate_id_1" not in existing_indexes:
                coll.create_index(
                    [("type", ASCENDING), ("predicate_id", ASCENDING)],
                    name="type_1_predicate_id_1",
                )
            if "updated_at_-1" not in existing_indexes:
                coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
        except OperationFailure as e:
            logger.warning(
                f"Could not create some indexes for {META_RELATIONS_COLLECTION_NAME}: {e}"
            )
        except Exception as e:
            logger.warning(
                f"Index creation skipped for {META_RELATIONS_COLLECTION_NAME}: {e}"
            )
        return coll
    return None


def get_agent_gmail_tokens_collection() -> Collection | None:
    """Returns the agent Gmail token collection and ensures indexes.

    Stores encrypted OAuth token payloads for agent Gmail profiles.
    IMPORTANT: Token content should never be logged.
    """

    db = get_db()
    if db is None:
        return None

    coll = db[AGENT_GMAIL_TOKENS_COLLECTION_NAME]
    try:
        existing_indexes = [idx["name"] for idx in coll.list_indexes()]

        if "profile_id_1_unique" not in existing_indexes:
            coll.create_index(
                [("profile_id", ASCENDING)], unique=True, name="profile_id_1_unique"
            )
        if "authorised_email_1" not in existing_indexes:
            coll.create_index(
                [("authorised_email", ASCENDING)], name="authorised_email_1"
            )
        if "updated_at_-1" not in existing_indexes:
            coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
    except OperationFailure as e:
        logger.warning(
            "Could not create some indexes for %s: %s",
            AGENT_GMAIL_TOKENS_COLLECTION_NAME,
            e,
        )
    except Exception as e:
        logger.warning(
            "Index creation skipped for %s: %s", AGENT_GMAIL_TOKENS_COLLECTION_NAME, e
        )

    return coll


def get_room_devices_collection() -> Collection | None:
    """Returns the room devices collection and ensures indexes.

    Stores provisioned meeting-room devices that can authenticate to the server.
    """

    db = get_db()
    if db is None:
        return None

    coll = db[ROOM_DEVICES_COLLECTION_NAME]
    try:
        existing_indexes = [idx["name"] for idx in coll.list_indexes()]

        if "device_id_1_unique" not in existing_indexes:
            coll.create_index(
                [("device_id", ASCENDING)], unique=True, name="device_id_1_unique"
            )
        if "organisation_id_1" not in existing_indexes:
            coll.create_index(
                [("organisation_id", ASCENDING)], name="organisation_id_1"
            )
        if "updated_at_-1" not in existing_indexes:
            coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
    except OperationFailure as e:
        logger.warning(
            "Could not create some indexes for %s: %s", ROOM_DEVICES_COLLECTION_NAME, e
        )
    except Exception as e:
        logger.warning(
            "Index creation skipped for %s: %s", ROOM_DEVICES_COLLECTION_NAME, e
        )

    return coll


# --- Example Usage (Optional, for testing) ---
# REFACTORING_NOTE: The __main__ block is updated to demonstrate access to the new collections
# and to reflect the new schema designs.
if __name__ == "__main__":
    # ... (keep existing people_coll access for transition demonstration) ...
    people_coll = get_people_collection()
    if people_coll is not None:
        print(
            f"Successfully accessed DEPRECATED '{PEOPLE_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
    else:
        print("Failed to access people collection. Check MongoDB connection.")

    print("\\n--- Testing Entities Collection ---")
    entities_coll = get_entities_collection()
    if entities_coll is not None:
        print(
            f"Successfully accessed '{ENTITIES_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
        # Example: Insert a test entity (if collection is empty)
        if entities_coll.count_documents({}) == 0:
            print(
                f"Attempting to insert test document into '{ENTITIES_COLLECTION_NAME}'..."
            )
            test_entity = {
                "vontology_path": "Concept/Test/TestSubject",  # Changed to string
                "name": "Test Entity Individual One",
                "description": "An entity for testing purposes.",
                "notes": "Initial test entry for generalized entities. Supports **Markdown**.",
                "attributes": {"test_attribute": "test_value", "status": "new"},
                "system_tags": ["test_data", "example"],
                "user_tags": ["important_test"],
                "linked_entities": [
                    {
                        "relationship_type": "related_to",
                        "target_entity_id": "some_other_entity_id_str",  # Placeholder
                        "target_vontology_path": "Concept/Test/AnotherTestSubject",
                        "target_name_for_display": "Another Test Entity",
                    }
                ],
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            }
            try:
                result = entities_coll.insert_one(test_entity)
                print(f"Inserted test entity with ID: {result.inserted_id}")
            except Exception as e:
                print(f"Error inserting test entity: {e}")
        else:
            print(
                f"Collection '{ENTITIES_COLLECTION_NAME}' already contains documents ({entities_coll.count_documents({})} total)."
            )
            # Optionally, print one document for inspection
            # print("Sample document:", entities_coll.find_one())
    else:
        print(f"Failed to access '{ENTITIES_COLLECTION_NAME}' collection.")

    print("\\n--- Testing User Entity Tracking Collection ---")
    user_tracking_coll = get_user_entity_tracking_collection()
    if user_tracking_coll is not None:
        print(
            f"Successfully accessed '{USER_ENTITY_TRACKING_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
        # Example: Insert a test tracking entry (if collection is empty or for a new user)
        test_user_id = "test-user-main-script"
        if user_tracking_coll.count_documents({"user_identifier": test_user_id}) == 0:
            print(
                f"Attempting to insert test document into '{USER_ENTITY_TRACKING_COLLECTION_NAME}' for user '{test_user_id}'..."
            )
            test_tracking_entry_type = {
                "user_identifier": test_user_id,
                "entity_identifier": "Concept/Test/TestType",  # Tracking a type
                "entity_kind": "type",
                "vontology_path": "Concept/Test/TestType",
                "entity_name_for_display": "Test Type",
                "last_accessed_timestamp": datetime.datetime.now(datetime.timezone.utc),
                "is_key_entity": True,
                "access_count": 5,
                "interaction_properties": {"view_mode": "list"},
            }
            test_tracking_entry_individual = {
                "user_identifier": test_user_id,
                "entity_identifier": "some_entity_id_str",  # Placeholder, would be an ObjectId string
                "entity_kind": "individual",
                "vontology_path": "Concept/Test/TestSubject",  # Type of the individual
                "entity_name_for_display": "Test Tracked Individual",
                "last_accessed_timestamp": datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1),
                "is_key_entity": False,
                "access_count": 1,
                "interaction_properties": {"filter_active": True},
            }
            try:
                result_type = user_tracking_coll.insert_one(test_tracking_entry_type)
                print(
                    f"Inserted test type tracking entry with ID: {result_type.inserted_id}"
                )
                result_individual = user_tracking_coll.insert_one(
                    test_tracking_entry_individual
                )
                print(
                    f"Inserted test individual tracking entry with ID: {result_individual.inserted_id}"
                )
            except Exception as e:
                print(f"Error inserting test tracking entry: {e}")
        else:
            print(
                f"Collection '{USER_ENTITY_TRACKING_COLLECTION_NAME}' already contains documents for user '{test_user_id}' ({user_tracking_coll.count_documents({'user_identifier': test_user_id})} total)."
            )
            # Optionally, print one document for inspection
            # print("Sample document:", user_tracking_coll.find_one({"user_identifier": test_user_id}))
    else:
        print(f"Failed to access '{USER_ENTITY_TRACKING_COLLECTION_NAME}' collection.")

    print("\n--- Testing Migrations Log Collection ---")
    migrations_log_coll = get_migrations_log_collection()
    if migrations_log_coll is not None:
        print(f"Successfully accessed '{MIGRATIONS_LOG_COLLECTION_NAME}' collection.")
        print(
            f"Indexes on migrations_log_coll: {migrations_log_coll.index_information()}"
        )
    else:
        print(f"Failed to access '{MIGRATIONS_LOG_COLLECTION_NAME}' collection.")

    print("\\n--- Testing Application Settings Collection ---")
    app_settings_coll = get_application_settings_collection()
    if app_settings_coll is not None:
        print(
            f"Successfully accessed '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection."
        )
        print(f"Indexes on app_settings_coll: {app_settings_coll.index_information()}")
        # Example: Insert or update a test setting
        try:
            app_settings_coll.update_one(
                {"setting_name": "test_setting"},
                {
                    "$set": {
                        "value": "test_value",
                        "updated_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
                upsert=True,
            )
            print("Upserted test_setting.")
            retrieved_setting = app_settings_coll.find_one(
                {"setting_name": "test_setting"}
            )
            print(f"Retrieved test_setting: {retrieved_setting}")
        except Exception as e:
            print(f"Error interacting with test_setting: {e}")
    else:
        print(f"Failed to access '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection.")

    # ... (keep existing interactions_coll and interaction_log_coll access for demonstration) ...
    interactions_coll = get_interactions_collection()
    if interactions_coll is not None:
        print(f"\\nSuccessfully accessed '{INTERACTIONS_COLLECTION_NAME}' collection.")
        # REFACTORING_NOTE: The schema for this collection will also be updated as per Sub-Task 1.4.
        # For now, just access. Future __main__ tests could reflect new schema.
    else:
        print(f"\\nFailed to access '{INTERACTIONS_COLLECTION_NAME}' collection.")

    interaction_log_coll = get_interaction_log_collection()
    if interaction_log_coll is not None:
        print(
            f"\\nSuccessfully accessed '{INTERACTION_LOG_COLLECTION_NAME}' collection."
        )
    else:
        print(f"\\nFailed to access '{INTERACTION_LOG_COLLECTION_NAME}' collection.")

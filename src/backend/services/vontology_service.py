# filepath: <PROJECT_ROOT>/src/backend/services/vontology_service.py
# REFACTORING_NOTE: This service will handle logic related to interacting with the Vontology
# file structure. This includes listing available types, fetching type definitions,
# and retrieving elaboration prompts associated with types.

import os
import re  # For parsing prompts
from typing import Optional, Dict, List, Any  # For type hinting

# REFACTORING_NOTE: Define Vontology base path, potentially from config later
VONTOLOGY_BASE_PATH = "knowledge/vontology"  # Relative to project root


class VontologyServiceError(Exception):
    pass


class VontologyTypeNotFoundError(VontologyServiceError):
    pass


# REFACTORING_NOTE: Functions for Vontology interaction will go here.


def get_vontology_root_path() -> str:
    # REFACTORING_NOTE: This helper function constructs the absolute path to the vontology root.
    # It assumes this service file is in src/backend/services and vontology is in knowledge/vontology.
    # This might need adjustment based on actual project structure or a configuration setting.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(
        os.path.join(current_dir, "..", "..", "..")
    )  # src/backend/services -> src/backend -> src -> project_root
    return os.path.join(project_root, VONTOLOGY_BASE_PATH)


def get_type_definition(vontology_path: str) -> Dict[str, Any]:
    """Retrieves the definition of a Vontology type, including its content and prompts.

    REFACTORING_NOTE: Implements the first part of Sub-Task 4.2.
    Reads the _Type.md file for the given vontology_path.
    Parses out general content and specific sections like "## Elaboration Prompts".

    Args:
        vontology_path: The Vontology path (e.g., "Concept/Person/Researcher").

    Returns:
        A dictionary containing:
            - "vontology_path": The input path.
            - "full_content": The entire raw Markdown content of the _Type.md file.
            - "elaboration_prompts": A list of strings, each an elaboration prompt.
            - "other_sections": A dictionary of other H2 sections and their content.

    Raises:
        VontologyTypeNotFoundError: If the _Type.md file for the path does not exist.
        VontologyServiceError: For other issues like file read errors.
    """
    if not vontology_path:
        raise VontologyServiceError("Vontology path cannot be empty.")

    # Construct the file path from the vontology_path
    # e.g., "Concept/Person/Researcher" -> "<root>/Concept/Person/Researcher/_Type.md"
    absolute_vontology_root = get_vontology_root_path()
    # Sanitize vontology_path to prevent directory traversal issues if it were user-supplied directly
    # For now, assume internal use where path is trusted or pre-validated.
    # Ensure no leading slashes on vontology_path if absolute_vontology_root is already absolute.
    clean_vontology_path = vontology_path.strip("/")
    type_file_path = os.path.join(
        absolute_vontology_root, clean_vontology_path, "_Type.md"
    )

    if not os.path.exists(type_file_path) or not os.path.isfile(type_file_path):
        raise VontologyTypeNotFoundError(
            f"Vontology type definition file not found at: {type_file_path} (for path: '{vontology_path}')"
        )

    try:
        with open(type_file_path, "r", encoding="utf-8") as f:
            full_content = f.read()
    except IOError as e:
        raise VontologyServiceError(
            f"Error reading Vontology type file '{type_file_path}': {e}"
        )

    # Parse content for prompts and other sections
    elaboration_prompts: List[str] = []
    other_sections: Dict[str, str] = {}
    current_section_title: Optional[str] = None
    current_section_content: List[str] = []

    # Split content by lines for easier parsing
    lines = full_content.splitlines()

    for line in lines:
        h2_match = re.match(r"^##\s+(.*)", line.strip())  # Match H2 headings
        if h2_match:
            # If there was a previous section, save it
            if current_section_title and current_section_content:
                if current_section_title.lower() == "elaboration prompts":
                    # Prompts are typically list items under the heading
                    for item_line in current_section_content:
                        prompt_match = re.match(r"^\s*[-\*\+]\s+(.*)", item_line)
                        if prompt_match:
                            elaboration_prompts.append(prompt_match.group(1).strip())
                else:
                    other_sections[current_section_title] = "\n".join(
                        current_section_content
                    ).strip()

            # Start new section
            current_section_title = h2_match.group(1).strip()
            current_section_content = []
        elif current_section_title:  # Only collect content if under a heading
            current_section_content.append(line)

    # Save the last section after the loop
    if current_section_title and current_section_content:
        if current_section_title.lower() == "elaboration prompts":
            for item_line in current_section_content:
                prompt_match = re.match(r"^\s*[-\*\+]\s+(.*)", item_line)
                if prompt_match:
                    elaboration_prompts.append(prompt_match.group(1).strip())
        else:
            other_sections[current_section_title] = "\n".join(
                current_section_content
            ).strip()

    return {
        "vontology_path": vontology_path,
        "full_content": full_content,
        "elaboration_prompts": elaboration_prompts,
        "other_sections": other_sections,
    }


def list_types(
    parent_path: Optional[str] = None,
    # criteria: Optional[Dict[str, Any]] = None, # Placeholder for future filtering
    # page: int = 1, # Placeholder for future pagination
    # per_page: int = 20 # Placeholder for future pagination
) -> List[Dict[str, Any]]:
    """Lists Vontology types, optionally under a given parent path.

    REFACTORING_NOTE: Implements the second part of Sub-Task 4.2.
    Walks the Vontology directory structure to find directories containing a _Type.md file.
    Currently, does not support complex criteria or pagination (placeholders added).

    Args:
        parent_path: Optional. If provided, lists types directly under this Vontology path
                     (e.g., "Concept/Person" would list "Researcher", "Artist" if they exist).
                     If None, lists top-level types (e.g., "Concept", "Event").

    Returns:
        A list of dictionaries, each representing a type:
            - "name": The name of the type (directory name).
            - "vontology_path": The full Vontology path to the type.
            - "has_children": Boolean, true if this type has subtypes.

    Raises:
        VontologyServiceError: If the specified parent_path is invalid or not found.
    """
    absolute_vontology_root = get_vontology_root_path()
    current_search_path = absolute_vontology_root

    if parent_path:
        clean_parent_path = parent_path.strip("/")
        current_search_path = os.path.join(absolute_vontology_root, clean_parent_path)
        if not os.path.exists(current_search_path) or not os.path.isdir(
            current_search_path
        ):
            raise VontologyServiceError(
                f"Parent Vontology path not found or is not a directory: {parent_path}"
            )

    found_types: List[Dict[str, Any]] = []

    try:
        for item_name in os.listdir(current_search_path):
            item_path = os.path.join(current_search_path, item_name)
            if os.path.isdir(item_path):
                # A directory is considered a Vontology type if it contains a _Type.md file
                type_marker_file = os.path.join(item_path, "_Type.md")
                if os.path.exists(type_marker_file) and os.path.isfile(
                    type_marker_file
                ):
                    # Construct the vontology_path relative to the VONTOLOGY_BASE_PATH
                    relative_item_path = os.path.relpath(
                        item_path, absolute_vontology_root
                    )
                    # Convert to forward slashes for consistent Vontology path format
                    type_vontology_path = relative_item_path.replace(os.sep, "/")

                    # Check if this type has children (subdirectories that are also types)
                    has_children = False
                    for sub_item_name in os.listdir(item_path):
                        sub_item_path = os.path.join(item_path, sub_item_name)
                        if os.path.isdir(sub_item_path) and os.path.exists(
                            os.path.join(sub_item_path, "_Type.md")
                        ):
                            has_children = True
                            break

                    found_types.append(
                        {
                            "name": item_name,
                            "vontology_path": type_vontology_path,
                            "has_children": has_children,
                        }
                    )
    except OSError as e:
        root_label = parent_path or "root"
        raise VontologyServiceError(
            f"Error listing Vontology types under '{root_label}': {e}"
        )

    # Sort by name for consistent ordering
    found_types.sort(key=lambda x: x["name"])
    return found_types


def search_types(
    query: str,
    search_in_content: bool = False,
    # page: int = 1, # Placeholder for future pagination
    # per_page: int = 20 # Placeholder for future pagination
) -> List[Dict[str, Any]]:
    """Searches for Vontology types by name and optionally within their _Type.md content.

    REFACTORING_NOTE: Implements the third part of Sub-Task 4.2.
    Recursively walks the Vontology directory structure.
    Matches the query (case-insensitive) against type names (directory names).
    If search_in_content is True, also searches within the full_content of _Type.md.
    Currently, does not support complex criteria or pagination (placeholders added).

    Args:
        query: The search string.
        search_in_content: If True, searches within the content of _Type.md files.
                           Defaults to False (search by type name only).

    Returns:
        A list of dictionaries, each representing a matched type:
            - "name": The name of the type.
            - "vontology_path": The full Vontology path to the type.
            - "has_children": Boolean, true if this type has subtypes.
            - "match_type": "name" or "content" or "name_and_content" indicating how the query matched.
            - "content_snippet": (Optional) A small snippet of content if matched in content.

    Raises:
        VontologyServiceError: For file system errors during search.
    """
    if not query:
        return []

    absolute_vontology_root = get_vontology_root_path()
    matching_types: List[Dict[str, Any]] = []
    query_lower = query.lower()

    # Helper function to recursively search
    def _recursive_search(current_dir_abs: str, current_vontology_prefix: str):
        try:
            for item_name in os.listdir(current_dir_abs):
                item_abs_path = os.path.join(current_dir_abs, item_name)
                if os.path.isdir(item_abs_path):
                    type_marker_file = os.path.join(item_abs_path, "_Type.md")
                    current_item_vontology_path = (
                        f"{current_vontology_prefix}/{item_name}"
                        if current_vontology_prefix
                        else item_name
                    )
                    current_item_vontology_path = current_item_vontology_path.strip("/")

                    if os.path.exists(type_marker_file) and os.path.isfile(
                        type_marker_file
                    ):
                        name_match = False
                        content_match = False
                        content_snippet = None

                        # Check name match
                        if query_lower in item_name.lower():
                            name_match = True

                        # Check content match if requested
                        if search_in_content:
                            try:
                                type_def = get_type_definition(
                                    current_item_vontology_path
                                )
                                if query_lower in type_def["full_content"].lower():
                                    content_match = True
                                    # Create a simple snippet
                                    try:
                                        match_index = (
                                            type_def["full_content"]
                                            .lower()
                                            .index(query_lower)
                                        )
                                        start = max(0, match_index - 30)
                                        end = min(
                                            len(type_def["full_content"]),
                                            match_index + len(query) + 30,
                                        )
                                        content_snippet = (
                                            "..."
                                            + type_def["full_content"][start:end]
                                            + "..."
                                        )
                                    except (
                                        ValueError
                                    ):  # Should not happen if query_lower in content
                                        pass
                            except (
                                VontologyServiceError
                            ):  # Ignore types that can't be read for content search
                                pass

                        match_type_str = None
                        if name_match and content_match:
                            match_type_str = "name_and_content"
                        elif name_match:
                            match_type_str = "name"
                        elif content_match:
                            match_type_str = "content"

                        if match_type_str:
                            # Check for children (re-using some logic from list_types)
                            has_children = False
                            for sub_item_name in os.listdir(item_abs_path):
                                sub_item_path = os.path.join(
                                    item_abs_path, sub_item_name
                                )
                                if os.path.isdir(sub_item_path) and os.path.exists(
                                    os.path.join(sub_item_path, "_Type.md")
                                ):
                                    has_children = True
                                    break

                            type_info = {
                                "name": item_name,
                                "vontology_path": current_item_vontology_path,
                                "has_children": has_children,
                                "match_type": match_type_str,
                            }
                            if content_snippet:
                                type_info["content_snippet"] = content_snippet
                            matching_types.append(type_info)

                    # Recursively search in subdirectories, even if current one is not a type itself
                    # This allows finding types nested under non-type folders if such structure exists.
                    _recursive_search(item_abs_path, current_item_vontology_path)
        except OSError as e:
            # Log or handle error, but try to continue searching other branches if possible
            # For simplicity, we raise if the root listing fails.
            if current_dir_abs == absolute_vontology_root:
                raise VontologyServiceError(f"Error searching Vontology types: {e}")
            # else: could log error for a sub-path and continue

    _recursive_search(absolute_vontology_root, "")

    # Sort by name for consistent ordering
    matching_types.sort(key=lambda x: x["name"])
    return matching_types


# REFACTORING_TEST_NOTE: Unit tests for this service will be in tests/backend/services/test_vontology_service.py
# They should mock file system operations (os.path.exists, os.listdir, open, etc.).

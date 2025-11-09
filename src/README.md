# Overview of the `src/` Directory

The `src/` directory is organized into several high-level subdirectories, each serving a specific role in the project architecture:

## backend/
Handles server-side logic, APIs, database interactions, language model interfaces, and Vontology (knowledge base) migration utilities.

## frontend/
Contains user interface code, including terminal-based and web-based interfaces for interacting with the system.

## knowledge/
Stores curated, structured, and persistent domain knowledge, such as ontologies and reference documents (e.g., Vontology).

## memories/
Manages dynamic, episodic, or user/system-generated data, including local and cloud-based memory storage and utilities for memory management.

## models/
Contains wrappers and integration code for various AI/ML models, including language, image, speech, video, and multimodal models.

## personas/
Defines agent personas representing specific task contexts or behaviors (e.g., organizer, research assistant).

## utilities/
Includes helper scripts and tools for maintenance and project management.

## workflows/
Describes task-specific processes that integrate models, memories, and personas to accomplish higher-level goals (e.g., chat LLM, tellvon).

This structure supports modular development and clear separation of concerns, making it easier to maintain and extend the project.
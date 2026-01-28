# TaskSpecification

**SubConcept Of**: PropositionalInformationThing

**Description**: A TaskSpecification is a declarative representation of work to be done. It captures the *what* of a task: title, description, requirements, acceptance criteria, priority, due date, and assignee. TaskSpecifications are distinct from their execution — the same specification could be executed multiple times or by different agents. This separation supports task delegation, tracking, and integration with external task management systems (Jira, Microsoft Tasks, Google Tasks, Trello).

**Salient Predicates**:
- hasAssignee (person or organisation responsible for the task)
- hasCreatedBy (person or agent who created the task)
- hasOriginatingConversation (the conversation context in which the task was identified)
- hasDueDate (optional deadline)
- hasPriority (low, medium, high, critical)
- hasTaskStatus (pending, in_progress, completed, cancelled, blocked)
- hasDescription (detailed task description via text relation)
- hasTitle (display name via hasName text relation)

**Integration Notes**:
- Tasks created by the agent during conversation should link to the originating conversation concept
- External system mappings (e.g., Jira issue key) can be stored as text relations
- Task status changes should be tracked for audit and notification purposes

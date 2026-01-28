# TaskExecution

**SubConcept Of**: Event

**Description**: A TaskExecution represents a specific instance of work being performed on a TaskSpecification. While the TaskSpecification captures *what* should be done, the TaskExecution captures *when*, *how*, and *by whom* it was actually done. This separation allows tracking multiple attempts, delegation changes, and historical audit trails.

**Salient Predicates**:
- executesTask (links to the TaskSpecification being executed)
- hasExecutor (the person or agent actually doing the work; may differ from original assignee)
- hasExecutionStatus (pending, in_progress, completed, failed, cancelled)
- hasStartTime (when execution began)
- hasEndTime (when execution completed or was cancelled)
- hasResult (outcome description or linked artefact)
- hasProgressNote (incremental updates during execution)

**Implementation Notes**:
- For simple use cases (single execution), TaskExecution may be implicit — the TaskSpecification itself can hold status
- TaskExecution becomes important for: retries, delegation, agent-executed tasks, audit requirements
- The BackgroundTaskRegistry (JVNAUTOSCI-1038) handles runtime execution; TaskExecution is the persistent record

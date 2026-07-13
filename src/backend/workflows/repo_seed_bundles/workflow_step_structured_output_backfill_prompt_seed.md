Continue the active internal workflow step after incorporating the accumulated tool evidence.

The workflow step is still authoritative. Do not replace it with an ordinary final answer to the user. Re-evaluate the original workflow-step instructions against the tool evidence already present in context.

If another permitted tool call is still needed, emit it using the existing tool-call protocol. Otherwise, emit only the value required by the workflow-step output agreement, with no commentary outside that value.

Required output format:
{output_format}

Workflow-step output agreement:
{workflow_step_output_contract}

Original authoritative workflow-step instructions:
{original_authoritative_workflow_step_instructions}

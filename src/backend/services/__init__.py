"""Service layer package exports.

Keep package imports lazy so callers such as workflow/Vontology bootstrap paths
do not accidentally import the entire service graph when they only need one
module. This avoids heavy startup side effects during MCP and workflow loading.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

_SERVICE_EXPORT_MODULES = {
    "annotation_extraction_service": ".annotation_extraction_service",
    "benchmark_suite_vontology_service": ".benchmark_suite_vontology_service",
    "concept_normalization": ".concept_normalization",
    "concept_service": ".concept_service",
    "episode_self_improvement_profile_vontology_service": ".episode_self_improvement_profile_vontology_service",
    "file_copy_diagram_interpretation_vontology_service": ".file_copy_diagram_interpretation_vontology_service",
    "knowledge_acquisition_profile_vontology_service": ".knowledge_acquisition_profile_vontology_service",
    "meta_relations_service": ".meta_relations_service",
    "minimal_imposition_benchmark_profile_vontology_service": ".minimal_imposition_benchmark_profile_vontology_service",
    "minimal_imposition_benchmark_service": ".minimal_imposition_benchmark_service",
    "minimal_imposition_runtime_profile_vontology_service": ".minimal_imposition_runtime_profile_vontology_service",
    "multilingual_concept_enrichment_schedule_bootstrap_service": ".multilingual_concept_enrichment_schedule_bootstrap_service",
    "multilingual_concept_enrichment_vontology_service": ".multilingual_concept_enrichment_vontology_service",
    "paper_recommendation_materialisation_service": ".paper_recommendation_materialisation_service",
    "paper_recommendation_delivery_service": ".paper_recommendation_delivery_service",
    "paper_recommendation_profile_vontology_service": ".paper_recommendation_profile_vontology_service",
    "paper_recommendation_review_service": ".paper_recommendation_review_service",
    "paper_recommendation_ranking_service": ".paper_recommendation_ranking_service",
    "paper_recommendation_vontology_service": ".paper_recommendation_vontology_service",
    "paper_recommendation_background_schedule_bootstrap_service": ".paper_recommendation_background_schedule_bootstrap_service",
    "paper_recommendation_workflow_vontology_service": ".paper_recommendation_workflow_vontology_service",
    "parent_specificity_schedule_bootstrap_service": ".parent_specificity_schedule_bootstrap_service",
    "parent_specificity_vontology_service": ".parent_specificity_vontology_service",
    "relation_elicitation_service": ".relation_elicitation_service",
    "represented_artefact_creation_workflow_vontology_service": ".represented_artefact_creation_workflow_vontology_service",
    "settings_service": ".settings_service",
    "skill_catalogue_service": ".skill_catalogue_service",
    "testing_workflow_vontology_service": ".testing_workflow_vontology_service",
    "text_value_service": ".text_value_service",
    "vontology_service": ".vontology_service",
    "workflow_continuation_service": ".workflow_continuation_service",
    "workflow_authoring_request_interpretation_vontology_service": ".workflow_authoring_request_interpretation_vontology_service",
    "workflow_gap_vontology_service": ".workflow_gap_vontology_service",
}

if TYPE_CHECKING:
    from . import annotation_extraction_service as annotation_extraction_service
    from . import (
        benchmark_suite_vontology_service as benchmark_suite_vontology_service,
    )
    from . import concept_normalization as concept_normalization
    from . import concept_service as concept_service
    from . import (
        episode_self_improvement_profile_vontology_service as episode_self_improvement_profile_vontology_service,
    )
    from . import (
        file_copy_diagram_interpretation_vontology_service as file_copy_diagram_interpretation_vontology_service,
    )
    from . import (
        knowledge_acquisition_profile_vontology_service as knowledge_acquisition_profile_vontology_service,
    )
    from . import meta_relations_service as meta_relations_service
    from . import (
        minimal_imposition_benchmark_profile_vontology_service as minimal_imposition_benchmark_profile_vontology_service,
    )
    from . import (
        minimal_imposition_benchmark_service as minimal_imposition_benchmark_service,
    )
    from . import (
        minimal_imposition_runtime_profile_vontology_service as minimal_imposition_runtime_profile_vontology_service,
    )
    from . import (
        multilingual_concept_enrichment_schedule_bootstrap_service as multilingual_concept_enrichment_schedule_bootstrap_service,
    )
    from . import (
        multilingual_concept_enrichment_vontology_service as multilingual_concept_enrichment_vontology_service,
    )
    from . import (
        paper_recommendation_background_schedule_bootstrap_service as paper_recommendation_background_schedule_bootstrap_service,
    )
    from . import (
        paper_recommendation_delivery_service as paper_recommendation_delivery_service,
    )
    from . import (
        paper_recommendation_materialisation_service as paper_recommendation_materialisation_service,
    )
    from . import (
        paper_recommendation_profile_vontology_service as paper_recommendation_profile_vontology_service,
    )
    from . import (
        paper_recommendation_review_service as paper_recommendation_review_service,
    )
    from . import (
        paper_recommendation_ranking_service as paper_recommendation_ranking_service,
    )
    from . import (
        paper_recommendation_vontology_service as paper_recommendation_vontology_service,
    )
    from . import (
        paper_recommendation_workflow_vontology_service as paper_recommendation_workflow_vontology_service,
    )
    from . import (
        parent_specificity_schedule_bootstrap_service as parent_specificity_schedule_bootstrap_service,
    )
    from . import (
        parent_specificity_vontology_service as parent_specificity_vontology_service,
    )
    from . import relation_elicitation_service as relation_elicitation_service
    from . import (
        represented_artefact_creation_workflow_vontology_service as represented_artefact_creation_workflow_vontology_service,
    )
    from . import settings_service as settings_service
    from . import skill_catalogue_service as skill_catalogue_service
    from . import testing_workflow_vontology_service as testing_workflow_vontology_service
    from . import text_value_service as text_value_service
    from . import vontology_service as vontology_service
    from . import workflow_continuation_service as workflow_continuation_service
    from . import (
        workflow_authoring_request_interpretation_vontology_service as workflow_authoring_request_interpretation_vontology_service,
    )
    from . import workflow_gap_vontology_service as workflow_gap_vontology_service


def __getattr__(name: str):
    module_path = _SERVICE_EXPORT_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
    value = import_module(module_path, __name__)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_SERVICE_EXPORT_MODULES.keys()))

__all__ = [
    "annotation_extraction_service",
    "benchmark_suite_vontology_service",
    "concept_normalization",
    "concept_service",
    "episode_self_improvement_profile_vontology_service",
    "file_copy_diagram_interpretation_vontology_service",
    "knowledge_acquisition_profile_vontology_service",
    "meta_relations_service",
    "minimal_imposition_benchmark_profile_vontology_service",
    "minimal_imposition_benchmark_service",
    "minimal_imposition_runtime_profile_vontology_service",
    "multilingual_concept_enrichment_schedule_bootstrap_service",
    "multilingual_concept_enrichment_vontology_service",
    "paper_recommendation_background_schedule_bootstrap_service",
    "paper_recommendation_delivery_service",
    "paper_recommendation_materialisation_service",
    "paper_recommendation_profile_vontology_service",
    "paper_recommendation_review_service",
    "paper_recommendation_ranking_service",
    "paper_recommendation_vontology_service",
    "paper_recommendation_workflow_vontology_service",
    "parent_specificity_schedule_bootstrap_service",
    "parent_specificity_vontology_service",
    "relation_elicitation_service",
    "represented_artefact_creation_workflow_vontology_service",
    "settings_service",
    "skill_catalogue_service",
    "testing_workflow_vontology_service",
    "text_value_service",
    "vontology_service",
    "workflow_authoring_request_interpretation_vontology_service",
    "workflow_continuation_service",
    "workflow_gap_vontology_service",
]

# Scholarly Work
ConceptID: ScholarlyWork
## Description
Scholarly Work refers to any intellectual artifact created with the intent of contributing to the body of knowledge within a specific domain, adhering to recognized academic or professional standards. These works encompass a wide range of formats, including written documents (e.g., journal articles, books, reports), digital creations (e.g., datasets, software), oral presentations (e.g., lectures, conference talks), and visual aids (e.g., posters). As artifacts, they are products of human intentionality and expertise, often undergoing peer review or equivalent evaluation to ensure quality and validity. In the context of ontologies like OpenCyc, Scholarly Work can be modeled as a subclass of InformationBearingObject, reflecting its role in encoding, storing, and communicating information. Scholarly Works typically have properties such as authorship, purpose, context of creation, and target audience, and they serve as reference points for subsequent inquiry, teaching, and application. These artifacts are distinguished by their systematic methodology, reproducibility, and alignment with epistemic and ethical norms, marking them as integral components of human knowledge systems.

[Citation: From ChatGPT, MJW, Prompt: Give me a one paragraph description of the concept of Scholarly Work (the artifact). Consider OpenCyc concepts where useful ]

For now, it's been modelled as a subtype of the OpenCyc Concept Scholarly Article but perhaps this relationship should be reviewed

**SubConcept Of**: ScholarlyArticle
**concept_id**: ScholarlyWork


## Note
These are derived from the BibTeX standard entry types, 
with some modifications and additions to better represent the diversity of scholarly work.

Subtypes, Fields, and Descriptions
| **Entity Type** | **Description** | **Required Fields** | **Optional Fields** |
|------------------|-----------------|----------------------|----------------------|
| **@article** | Represents a journal article. Suitable for peer-reviewed works in academic journals. | `author`, `title`, `journal`, `year` | `volume`, `number`, `pages`, `month`, `note`, `doi`, `url` |
| **@book** | Represents a book with a specific publisher. Useful for monographs or edited volumes. | `author` or `editor`, `title`, `publisher`, `year` | `volume`, `series`, `address`, `edition`, `month`, `note`, `isbn` |
| **@inbook** | Refers to a part of a book, such as a chapter, section, or specific page range. | `author` or `editor`, `title`, `chapter` or `pages`, `publisher`, `year` | `volume`, `series`, `address`, `edition`, `month`, `note` |
| **@incollection** | Represents a contribution to a collection, such as an essay in an edited volume. | `author`, `title`, `booktitle`, `publisher`, `year` | `editor`, `volume`, `series`, `chapter`, `pages`, `address`, `edition`, `month`, `note` |
| **@inproceedings** | Represents a paper in conference proceedings. Common in computer science and engineering. | `author`, `title`, `booktitle`, `year` | `editor`, `volume`, `series`, `pages`, `address`, `month`, `organization`, `publisher`, `note` |
| **@proceedings** | Refers to the entire conference proceedings (not an individual paper). | `title`, `year` | `editor`, `volume`, `series`, `address`, `month`, `organization`, `publisher`, `note` |
| **@techreport** | Represents technical reports, often published by universities or research labs. | `author`, `title`, `institution`, `year` | `type`, `number`, `address`, `month`, `note` |
| **@phdthesis** | Represents a PhD dissertation or thesis. | `author`, `title`, `school`, `year` | `address`, `month`, `note` |
| **@mastersthesis** | Represents a Master’s dissertation or thesis. | `author`, `title`, `school`, `year` | `address`, `month`, `note` |
| **@booklet** | Represents printed or online works without a formal publisher. | `title` | `author`, `howpublished`, `address`, `month`, `year`, `note` |
| **@dataset** | Represents academic datasets, often deposited in repositories for reuse. | `author`, `title`, `year` | `publisher`, `doi`, `url`, `version`, `repository` |
| **@preprint** | Represents works uploaded to preprint repositories (e.g., arXiv) before peer review. | `author`, `title`, `year`, `archive` | `identifier`, `url` |
| **@patent** | Represents patents, typically tied to academic or industrial research. | `inventor`, `title`, `year` | `patentnumber`, `country`, `url` |
| **@software** | Represents software, tools, or libraries developed as part of scholarly work. | `author`, `title`, `year` | `version`, `repository`, `url` |
| **@blog** | Represents blog posts used for science communication or commentary. | `author`, `title`, `year`, `blogtitle` | `url` |
| **@video** | Represents videos such as lectures, presentations, or tutorials. | `author`, `title`, `year` | `platform`, `url`, `duration` |
| **@course** | Represents online courses, often hosted on MOOC platforms like Coursera or edX. | `instructor`, `title`, `year` | `platform`, `url` |
| **@poster** | Represents academic posters, usually presented at conferences or workshops. | `author`, `title`, `year` | `event`, `location`, `url` |
| **@podcast** | Represents podcast episodes featuring scholarly discussions or interviews. | `host`, `title`, `year` | `guest`, `episode`, `url` |
| **@grant** | Represents research grants or funding awards. | `title`, `principal_investigator`, `year` | `agency`, `grantnumber`, `url` |
| **@whitepaper** | Represents institutional or policy white papers. | `author`, `title`, `year` | `organization`, `url` |
| **@report** | Represents research reports, often published by think tanks or government agencies. | `author`, `title`, `year` | `institution`, `url` |
| **@standard** | Represents technical standards or specifications. | `organization`, `title`, `year` | `number`, `url` |
| **@thesis** | Represents any type of academic thesis, such as a PhD, Master’s, or Bachelor’s thesis. | `author`, `title`, `school`, `year` | `type`, `address`, `month`, `note` |
| **@lecture** | Represents lectures or talks given by scholars or experts. | `speaker`, `title`, `year` | `event`, `location`, `url` |
| **@presentation** | Represents academic presentations, often given at conferences or seminars. | `author`, `title`, `year` | `event`, `location`, `url` |
| **@workshop** | Represents academic workshops or training sessions. | `organizer`, `title`, `year` | `event`, `location`, `url` |
| **@manual** | Represents manuals or guides related to scholarly work. | `author`, `title`, `year` | `organization`, `url` |

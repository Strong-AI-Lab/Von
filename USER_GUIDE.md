# Von User Guide
[comment]: # (DO NOT DELETE THIS COMMENT AND KEEP FOR THE SUBSEQUENT REVISION AS WELL. Find all my comments indicated with "[comment]:" and revise this USER.GUIDE.md. Importantly, when review my comments placed in different sections in this .md file, you must apply the changes according to the comments to the entire writings and contents in this .md file if necessary. For example, if a comment asks to delete a section then the table of contents must be modified accordingly too. ])

Welcome to Von! This comprehensive guide will help you understand and use Von effectively for your research and knowledge management needs.

---

## Table of Contents

1. [Introduction](#1-introduction)
   - 1.1 [What is Von?](#11-what-is-von)
   - 1.2 [Who Should Use Von?](#12-who-should-use-von)
   - 1.3 [Core Concepts Overview](#13-core-concepts-overview)
2. [Quick Start: Exploring Von's Interface](#2-quick-start-exploring-vons-interface)
   - 2.1 [Starting Von](#21-starting-von)
   - 2.2 [Explore the Vontology](#22-explore-the-vontology)
   - 2.3 [Building Your First Knowledge Base: A Complete Workflow](#23-building-your-first-knowledge-base-a-complete-workflow)
   - 2.4 [Next Steps: Expanding Your Knowledge Base](#24-next-steps-expanding-your-knowledge-base)

---

## 1. Introduction

### 1.1 What is Von?

Von is an AI-agent system designed specifically for academic research and knowledge management. It combines the power of large language models (LLMs) with a structured knowledge organisation system called Vontology to help researchers manage complex information, explore scholarly literature, and maintain verifiable knowledge bases.

Unlike traditional AI assistants that rely solely on statistical pattern recognition, Von uses a **neuro-symbolic approach**. This means it combines:
- **Neural AI** (LLMs like GPT-4&5, Ollama models, or Google Gemini) for natural language understanding and generation
- **Symbolic AI** (the Vontology ontology) for structured, verifiable knowledge representation

This combination significantly reduces AI hallucinations by grounding responses in your curated knowledge base, making Von particularly valuable for research contexts where accuracy and provenance matter.

Von aims to help you:
- **Organise research knowledge** hierarchically using concepts and relationships
- **Chat with AI** that understands your research domain and knowledge base
- **Manage entities** like research papers, notes, datasets, and people
- **Extract and annotate** important information from texts
- **Search and import** scholarly articles from arXiv
- **Track provenance** of every piece of information in your knowledge base

### 1.2 Who Should Use Von?

Von is designed for:

**Researchers and Academics** who need to:
- Manage large bodies of research literature and notes
- Organise domain knowledge hierarchically
- Extract key concepts and relationships from papers
- Maintain verifiable research knowledge bases
- Collaborate with AI while maintaining factual accuracy

**Knowledge Workers** who:
- Deal with complex, interconnected information
- Need to track the source and context of knowledge
- Want AI assistance without risking misinformation
- Value structured knowledge organisation

**Research Teams** who:
- Share domain ontologies and conceptual frameworks
- Annotate and discuss research materials collectively
- Need consistent terminology and concept definitions
- Want to build institutional knowledge over time

### 1.3 Core Concepts Overview

Before diving into Von's features, it helps to understand a few key concepts:

**Vontology**: The hierarchical knowledge organisation system at Von's core. Think of it as a tree of concepts where each concept can have:
- **Parent concepts** (more general categories)
- **Child concepts** (more specific instances)
- **Predicates** (relationships to other concepts or values)
- **Properties** (like names, descriptions, and metadata)

Example hierarchy:
```
#V#research_artifact (root concept)
  └─ #V#scholarly_work
      └─ #V#research_paper
          ├─ #V#conference_paper
          └─ #V#journal_article
```

**Concepts**: Nodes in the Vontology tree representing ideas, categories, or entities. Each concept has a unique identifier (like `#V#person`) and can have multiple names in different languages.

**Predicates**: Relationships between concepts or concepts and values. Examples:
- `subconceptOf`: Defines the hierarchy (#V#conference_paper is a subconceptOf #V#research_paper)
- `hasName`: Associates text names with concepts
- `cites`: Links research papers that cite each other
- `authorOf`: Links researchers to their publications
- `usesMethodology`: Links papers to research methods employed

**Entities**: Concrete instances or research artifacts you're tracking, such as:
- Research papers (e.g., "Attention Is All You Need")
- Datasets (e.g., ImageNet, WMT2014)
- Researchers (e.g., Geoffrey Hinton, Yoshua Bengio)
- Research projects and experiments
- Literature review notes

Entities are linked to concepts (e.g., the paper "Attention Is All You Need" would be linked to #V#conference_paper).

**Annotations**: Highlighted portions of text extracted from entities, often with automatically detected predicates showing relationships mentioned in the text.

[Screenshot: Von interface overview showing Vontology tree, entity list, and chat interface] (⚠️)

---

## 2. Quick Start: Exploring Von's Interface

> **Note**: Before starting, ensure you have completed Von's installation and configuration as described in the [main README.md](../README.md).

This quick walkthrough will introduce you to Von's core features through hands-on exploration of the interface.

### 2.1 Starting Von

**On Windows**:
```powershell
.\run.ps1
```

**On Linux/macOS**:
```bash
./run.sh
```

**Open your browser** to `http://localhost:5001`

[Screenshot: Von home interface with Vontology tree on left, main panel in center, chat on right]

### 2.2 Explore the Vontology

> **Note**: Von **automatically creates a root "Thing" concept** when you first start with an empty database. This follows OWL (Web Ontology Language) conventions where "Thing" serves as the universal root of all ontologies.

The Vontology panel shows your hierarchical tree of concepts. Depending on your setup choice, you'll see:

**Option A: Empty Database (Auto-Created Root)**
- Running Von for the first time automatically creates **"Thing"** as your root concept
- Thing is the universal parent for all other concepts
- You'll see a single node in the tree ready for you to add child concepts
- **Stored in**: Local MongoDB (`von_db` database, `concepts` collection)

**Option B: Sample Knowledge Loaded**
- If you ran `python src/utilities/init_database.py --full-setup` during setup
- You'll see **"Thing"** with pre-loaded AI-related concepts:
  - Artificial Intelligence
  - Machine Learning
  - Natural Language Processing
  - Deep Learning
  - Large Language Model
- These serve as examples you can explore or delete
- **Stored in**: Local MongoDB database, loaded from `sample_knowledge/starter_ontology.json`

**Exploring Concepts**:

1. **Navigate the tree** - Click on any concept to view its details:
   - Concept description, notes, and attributes
   - Parent concepts (more general categories)
   - Child concepts (more specific subtypes)
   - Predicates and relationships
   - Linked entities (concrete instances)

2. **Expand/collapse branches**:
   - Click **▶** icon to expand a concept's children
   - Click **▼** icon to collapse branches
   - Use search to find specific concepts quickly

3. **Understand the hierarchy**:
   ```
   Thing (root - auto-created)
     └─ Your Concepts
         └─ More Specific Concepts
             └─ Even More Specific...
   ```

**Navigation Tips**:
- **Right-click** on concepts for quick actions (create child, delete, etc.)
- **Click concept names** to view full details in the main panel
- **Use search** to quickly locate concepts by name
- **Star key concepts** to mark them as favorites for quick access

[Screenshot: Vontology tree showing Thing root with expandable children]

### 2.3 Building Your First Knowledge Base: A Complete Workflow

This section will walk you through building your first research knowledge structure in Von and demonstrate how structured knowledge improves AI interactions. We'll use a practical scenario: organizing knowledge about neural network architectures.

> **Important**: Before starting, ensure you've completed the database setup (**option A - empty database**) from the README.md. You should see the "Thing" root concept in your Vontology tree.

#### Step 1: Understand the Baseline (Before Adding Knowledge)

Let's first see how Von responds without domain-specific knowledge.

**Try asking Von in the Chat tab**:
> "Explain the key innovations in transformer architectures for natural language processing"

**Von's response (without domain knowledge)**:
Von will provide a general answer based on the LLM's training data, but it won't be grounded in your specific research context, papers you've read, or your own conceptual framework.

#### Step 2: Create Your Research Domain Concept

Now let's build structured knowledge. Start by creating a concept for your research area.

**Where concepts are stored**: All concepts are saved in your MongoDB database (`von_db.concepts` collection) and linked via the Vontology hierarchy.

1. **Navigate to the Vontology tab** (left panel)
2. **Click on "Thing"** to select it as the parent
3. **Click "Create Type"** button (in the concept creation panel on the right)
4. **Fill in the form**:
   - **Name**: `Neural Network Architecture`
   - **Description**: `Computational models inspired by biological neural networks, used for machine learning tasks`
   - **Notes** (optional): `Parent concept for organizing different neural network architectures in my research`
5. **Click "Create Type"**

**Result**: 
- Your concept is created with ID `#V#neural_network_architecture`
- It appears under "Thing" in the Vontology tree
- It's stored in MongoDB with full metadata (description, notes, attributes, relationships)

[Screenshot: Create concept form with neural network architecture details]

**What just happened**:
- ✅ Created concept in `von_db.concepts` collection
- ✅ Created name entry in `von_db.text_relations` collection (predicate: `hasName`)
- ✅ Established parent-child relationship: Thing → Neural Network Architecture
- ✅ Auto-generated concept_id: `#V#neural_network_architecture`

#### Step 3: Build Your Concept Hierarchy

Now add more specific subconcepts to organize different architecture types.

1. **Select** `Neural Network Architecture` in the tree (click on it)
2. **In the concept creation panel**, you'll see it's now the parent
3. **Click "Create Type"** and create these subconcepts (repeat for each):

   **Concept 1**: Transformer Architecture
   - **Name**: `Transformer Architecture`
   - **Description**: `Attention-based architecture that processes sequences in parallel, introduced in "Attention Is All You Need" (2017)`
   - **ID**: Auto-generated as `#V#transformer_architecture`

   **Concept 2**: Recurrent Neural Network
   - **Name**: `Recurrent Neural Network`
   - **Description**: `Neural networks with loops for processing sequential data, suitable for time series and NLP tasks`
   - **ID**: Auto-generated as `#V#recurrent_neural_network`

   **Concept 3**: Convolutional Neural Network
   - **Name**: `Convolutional Neural Network`
   - **Description**: `Networks using convolution operations, primarily for computer vision and spatial data`
   - **ID**: Auto-generated as `#V#convolutional_neural_network`

**Your hierarchy now looks like** (in MongoDB and the Vontology UI):
```
Thing (#V#thing)
  └─ Neural Network Architecture (#V#neural_network_architecture)
      ├─ Transformer Architecture (#V#transformer_architecture)
      ├─ Recurrent Neural Network (#V#recurrent_neural_network)
      └─ Convolutional Neural Network (#V#convolutional_neural_network)
```

**Database structure**:
- Each concept is a document in `von_db.concepts`
- Relationships stored in `relationships.is_a_type_of` and `relationships.has_subtype` fields
- Names stored in `von_db.text_relations` with `predicate: "hasName"`

[Screenshot: Expanded Vontology tree showing neural network hierarchy]

#### Step 4: Add a Research Paper Entity

Now let's add a concrete research paper to your knowledge base.

> **Note**: Entities are instances of concepts, representing real-world artifacts like papers, datasets, or people.

1. **Navigate to "Entities"** tab in the main panel
2. **Click "Add Entity"** or "New Entity"
3. **Fill in the details**:
   - **Title**: `Attention Is All You Need`
   - **Type**: Select `Transformer Architecture` from the dropdown
   - **Content**:
     ```
     The dominant sequence transduction models are based on complex recurrent or
     convolutional neural networks. The best performing models also connect the
     encoder and decoder through an attention mechanism. We propose a new simple
     network architecture, the Transformer, based solely on attention mechanisms,
     dispensing with recurrence and convolutions entirely.
     ```
   - **Authors**: Vaswani, A., Shazeer, N., Parmar, N., et al.
   - **Publication Date**: 2017-06-12
   - **URL**: https://arxiv.org/abs/1706.03762
4. **Click "Save"**

**Result**: 
- The paper is stored in MongoDB (likely in `von_db.entities` or similar collection)
- It's linked to `#V#transformer_architecture` via `is_an_instance_of` relationship
- Von now knows about this specific paper in your research domain

[Screenshot: Entity creation form filled with Attention Is All You Need details]

#### Step 5: Create an Annotation to Extract Key Knowledge

Let's highlight and extract important information from the paper.

1. **Open the entity** you just created
2. **Select this text**: "The Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely"
3. **Click "Annotate"** button
4. **Add notes**: "Key innovation: eliminates recurrence and convolution, uses only attention"
5. **Review detected predicates** (Von may auto-detect relationships)
6. **Click "Save"**

**Result**: The key claim is now extracted and searchable.

[Screenshot: Annotation interface showing highlighted text and notes]

#### Step 6: Experience the Difference (After Adding Knowledge)

Now let's ask Von the **same question** we started with, but now it can ground its response in your curated knowledge base.

**Ask Von again in the Chat tab**:
> "Explain the key innovations in transformer architectures for natural language processing"

**Von's response (with your knowledge base)**:
```
Based on your Vontology and entities, transformer architectures represent a significant
innovation in neural network design. According to the paper "Attention Is All You Need"
in your knowledge base (which you've classified under #V#transformer_architecture), 
the key innovation is that transformers are "based solely on attention mechanisms, 
dispensing with recurrence and convolutions entirely."

This differentiates transformers from the other architectures in your Vontology:
- #V#recurrent_neural_network - which use loops for sequential processing
- #V#convolutional_neural_network - which use convolution operations

The elimination of recurrence allows for better parallelization during training, which
was a major advancement for NLP tasks. This is evidenced by the paper you've stored,
authored by Vaswani et al. (2017).
```

**Key Improvements**:
- ✅ **Grounded in your research**: References specific papers you've read and stored
- ✅ **Uses your conceptual framework**: Relates to your Vontology structure (Thing → Neural Network Architecture → Transformer)
- ✅ **Cites sources**: Points to `#V#transformer_architecture` concept and "Attention Is All You Need" entity
- ✅ **Contextual comparisons**: Compares with other concepts in your knowledge base
- ✅ **Verifiable**: All claims traceable to your MongoDB database
- ✅ **Reduced hallucination**: Answers constrained by your curated knowledge

**What's Happening Under the Hood**:
1. Von queries your MongoDB database (`von_db.concepts`, `von_db.text_relations`, entities)
2. Retrieves relevant concepts and relationships from the Vontology hierarchy
3. Includes entity content (your paper abstracts) as context for the LLM
4. LLM generates response grounded in this structured knowledge
5. Result: Accurate, traceable, context-aware answers

#### What You've Accomplished

**Congratulations!** You've completed the full knowledge-building workflow. You now have:

**In Your MongoDB Database (`von_db`)**:
- ✓ **Thing root concept** (auto-created by Von following OWL standards)
- ✓ **Neural Network Architecture hierarchy** (4 concepts with parent-child relationships)
- ✓ **Text relations** storing concept names (in `text_relations` collection, no duplicates)
- ✓ **Research paper entity** with full metadata and content
- ✓ **Annotations** extracting key claims from the paper

**In Your Von Interface**:
- ✓ Structured Vontology tree viewable in the left panel
- ✓ Concept details accessible by clicking nodes
- ✓ Entity management through the Entities tab
- ✓ AI chat grounded in your curated knowledge

This is the foundation of **neuro-symbolic AI**: combining structured knowledge (your Vontology stored in MongoDB) with neural language models to reduce hallucinations and increase accuracy.

### 2.4 Next Steps: Expanding Your Knowledge Base

Now that you understand the workflow and how data is stored in MongoDB, you can continue building your research knowledge:

**Add More Concepts**:
- Create subconcepts under existing concepts (e.g., BERT, GPT, T5 under Transformer Architecture)
- Add parallel hierarchies for other research areas
- Define custom predicates to link related concepts
- **Remember**: All stored in `von_db.concepts` with automatic text_relation creation

**Import More Entities**:
- Add more research papers from your reading list
- Import papers directly from arXiv (see section 3.5)
- Create entities for datasets, researchers, and projects
- Link entities to appropriate concepts in your Vontology

**Extract Knowledge Through Annotations**:
- Annotate methodology sections to capture research methods
- Highlight key results and findings
- Mark definitions of important terms
- Build a searchable knowledge repository

**Use Von for Research Tasks**:
- Ask Von to summarize themes across multiple papers (it queries your MongoDB database)
- Request comparisons between different approaches in your knowledge base
- Query for papers using specific methodologies you've annotated
- Explore relationships between concepts in your Vontology hierarchy

**Backup Your Knowledge** (Important!):
Your knowledge is stored in MongoDB. To backup:
```powershell
# Backup entire von_db database
mongodump --db von_db --out ./backups/$(date +%Y%m%d)

# Restore from backup
mongorestore --db von_db ./backups/20250113/von_db
```

**Sample Knowledge Reference**:
If you loaded the sample knowledge (`python src/utilities/init_database.py --full-setup`), you can:
- Explore the pre-loaded AI concepts as examples
- Extend them with your own research
- Delete them and start fresh if preferred
- Use them as templates for your own domain

**Congratulations!** You've completed the quick start walkthrough. You now understand:
- ✓ How Von auto-creates the "Thing" root concept following OWL standards
- ✓ How to build a structured Vontology hierarchy stored in MongoDB
- ✓ The workflow for creating concepts (stored in `von_db.concepts`)
- ✓ How names are stored via text_relations (preventing duplicates)
- ✓ How annotations extract key knowledge from entities
- ✓ The power of grounding AI responses in curated, verifiable knowledge
- ✓ The before/after impact of structured knowledge on AI interactions
- ✓ Where your data lives (MongoDB `von_db` database) and how to back it up

---

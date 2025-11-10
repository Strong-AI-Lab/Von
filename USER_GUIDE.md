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

### 2.2 Explore the Vontology  (incomplete ⚠️)

> **Note for New Users**: When you first start Von, your Vontology will be **empty**. This is intentional—Von provides you with a blank canvas to build your own research knowledge structure. The following sections will guide you through creating your first concepts and organizing your research domain. (⚠️)

The Vontology panel shows your hierarchical tree of concepts. As a new user, you'll see an empty tree, ready for you to populate with concepts relevant to your research.

**Your First Steps**:

1. **Start with a root concept** - You'll create a top-level concept for your research domain (we'll show you how in section 2.3)
2. **Build your hierarchy** - Add child concepts under your root to organize knowledge from general to specific
3. **View concept details** - Once you've created concepts, click on them to see:
   - Concept description, note, type, and content
   - Parent concepts (more general)
   - Child concepts (more specific)
   - Predicates and relationships
   - Linked entities (instances of the concept)

**Navigation Tips** (once you have concepts):
- Click **▶** icon to expand a concept's children
- Click **▼** icon to collapse branches
- Click concept names to view full details in the main panel

[Screenshot: Vontology tree navigation with expanded branches]

### 2.3 Building Your First Knowledge Base: A Complete Workflow

This section will walk you through building your first research knowledge structure in Von and demonstrate how structured knowledge improves AI interactions. We'll use a practical scenario: organizing knowledge about neural network architectures.

#### Step 1: Understand the Baseline (Before Adding Knowledge)

Let's first see how Von responds without any domain knowledge.

**Try asking Von**:
> "Explain the key innovations in transformer architectures for natural language processing"

**Von's response (without knowledge base)**:
Von will provide a general answer based on the LLM's training data, but it won't be grounded in your specific research context, papers you've read, or your own conceptual framework.

#### Step 2: Create Your Root Concept

Now let's build structured knowledge. Start by creating a foundational concept for your research domain.
[comment]: # (where in local device is the created concept stored?)

1. **Navigate to the Vontology panel** (left side)
2. **Click "Create Root Concept"** button (since your tree is empty)
3. **Fill in the form**:
   - **Name**: "Neural Network Architecture"
   - **Description**: "Computational models inspired by biological neural networks, used for machine learning tasks"
   - **ID**: Auto-generated as `#V#neural_network_architecture`
4. **Click "Create"**

**Result**: Your first concept appears as the root of your Vontology tree.

[Screenshot: Create concept form with neural network architecture details]

#### Step 3: Build Your Concept Hierarchy

Now add more specific subconcepts to organize different architecture types.

1. **Select** `#V#neural_network_architecture` in the tree
2. **Click "Add Child Concept"**
3. **Create these subconcepts** (repeat the process for each):

   **Concept 1**: Transformer Architecture
   - **Name**: "Transformer Architecture"
   - **Description**: "Attention-based architecture that processes sequences in parallel"
   - **ID**: `#V#transformer_architecture`

   **Concept 2**: Recurrent Neural Network
   - **Name**: "Recurrent Neural Network"
   - **Description**: "Neural networks with loops for processing sequential data"
   - **ID**: `#V#recurrent_neural_network`

   **Concept 3**: Convolutional Neural Network
   - **Name**: "Convolutional Neural Network"
   - **Description**: "Networks using convolution operations, primarily for computer vision"
   - **ID**: `#V#convolutional_neural_network`

**Your hierarchy now looks like**:
```
#V#neural_network_architecture
  ├─ #V#transformer_architecture
  ├─ #V#recurrent_neural_network
  └─ #V#convolutional_neural_network
```

[Screenshot: Expanded Vontology tree showing neural network hierarchy]

#### Step 4: Add a Research Paper Entity

Now let's add a concrete research paper to your knowledge base.

1. **Navigate to "Entities"** tab in the main panel
2. **Click "Add Entity"** or "New Entity"
3. **Fill in the details**:
   - **Title**: "Attention Is All You Need"
   - **Type**: Select `#V#transformer_architecture` from the dropdown
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

**Result**: The paper is now in your knowledge base, linked to `#V#transformer_architecture`.

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

Now let's ask Von the **same question** we started with.

**Ask Von again**:
> "Explain the key innovations in transformer architectures for natural language processing"

**Von's response (with knowledge base)**:
```
Based on your Vontology and entities, transformer architectures represent a significant
innovation in neural network design. According to the paper "Attention Is All You Need"
in your knowledge base (linked to #V#transformer_architecture), the key innovation is
that transformers are "based solely on attention mechanisms, dispensing with recurrence
and convolutions entirely."

This differentiates transformers from the other architectures in your Vontology:
- #V#recurrent_neural_network - which use loops for sequential processing
- #V#convolutional_neural_network - which use convolution operations

The elimination of recurrence allows for better parallelization during training, which
was a major advancement for NLP tasks.
```

**Key Improvements**:
- ✅ **Grounded in your research**: References specific papers you've read
- ✅ **Uses your conceptual framework**: Relates to your Vontology structure
- ✅ **Cites sources**: Points to `#V#transformer_architecture` and your annotated paper
- ✅ **Contextual comparisons**: Compares with other concepts in your knowledge base

#### What You've Accomplished

**Congratulations!** You've completed the full knowledge-building workflow. You now have:
- ✓ A structured Vontology hierarchy for neural network architectures
- ✓ A research paper entity with full metadata
- ✓ An annotation extracting key claims
- ✓ AI responses grounded in your curated knowledge

This is the foundation of neuro-symbolic AI: combining structured knowledge (your Vontology) with neural language models to reduce hallucinations and increase accuracy.

### 2.4 Next Steps: Expanding Your Knowledge Base

Now that you understand the workflow, you can continue building your research knowledge:

**Add More Concepts**:
- Create subconcepts under `#V#transformer_architecture` (e.g., BERT, GPT, T5)
- Add parallel hierarchies for other research areas
- Define custom predicates to link related concepts

**Import More Entities**:
- Add more research papers from your reading list
- Import papers directly from arXiv (see section 3.5)
- Create entities for datasets, researchers, and projects

**Extract Knowledge Through Annotations**:
- Annotate methodology sections to capture research methods
- Highlight key results and findings
- Mark definitions of important terms

**Use Von for Research Tasks**:
- Ask Von to summarize themes across multiple papers
- Request comparisons between different approaches
- Query for papers using specific methodologies

**Congratulations!** You've completed the quick start walkthrough. You now understand:
- ✓ How to build a structured Vontology hierarchy
- ✓ The workflow for creating concepts and entities
- ✓ How annotations extract key knowledge
- ✓ The power of grounding AI responses in curated knowledge
- ✓ The before/after impact of structured knowledge on AI interactions

---

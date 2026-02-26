# Von - AI-Agent System for Academic Research

This is the repository for the open source collaborative development of **Von**, initiated by the Strong AI Lab (SAIL) in the Natural Artificial and Organisational Intelligence Institute at the University of Auckland.

**Von** is an AI-agent system designed to help academic researchers manage knowledge, conduct research, and interact with AI systems in a structured, reliable, and academically rigorous manner. By combining ontology-based knowledge organisation, Von bridges the gap between implicit knowledge in Large Language Models (LLMs) and explicit, verifiable research structures.

## What is Von?

Von (or vonNeumarkt) provides researchers with AI-enriched tools that enhance academic workflows through systematic knowledge management and confidence-aware AI assistance. At its core is **Vontology**—an ontology (a formal representation of knowledge that defines concepts, their properties, and relationships) that structures information hierarchically and tracks provenance, ensuring every piece of knowledge is verifiable and contextualised.

**Why ontologies are the missing piece for AI agents:** Modern AI systems face a critical challenge—while LLMs excel at pattern recognition and flexible understanding, they suffer from hallucinations (generating plausible but false information) because they rely on statistical probabilities rather than verifiable facts. The future of reliable AI agents lies in **neuro-symbolic systems** that combine neural networks with symbolic reasoning.

**Von embodies this neuro-symbolic approach:** LLMs handle unstructured data and natural language understanding, while Vontology provides a factual, logical backbone—a semantic network of concepts and relationships dating back to Aristotelian knowledge organisation. This combination has been shown to reduce hallucinations to near-zero in certain applications by grounding AI responses in explicit, verifiable knowledge structures.

**In practice, this means:** Researchers can distinguish certain facts from uncertain AI suggestions, track how information relates across domains, build verifiable knowledge bases that grow more reliable over time, and maintain provenance for every piece of information. Whether you're conducting systematic literature reviews, mapping research networks, or managing complex entities (people, scholarly works, concepts, institutions), Von provides the structured foundation and AI assistance to work more effectively—without the usual risks of AI-generated misinformation.


## ✨ Key Features

- 🧠 **Vontology Knowledge Organisation** - Hierarchical ontology system for structuring research concepts, entities, and relationships with provenance tracking
- 📚 **Entity Management System** - Comprehensive tracking of researchers, scholarly works, institutions, and concepts with relationship mapping
- 📝 **Text Annotation & Extraction** - Systematic annotation of research materials with salient predicate detection via LLM
- 🔬 **Scholarly Article Integration** - MCP integration (e.g., arXiv MCP server) for searching, importing, and organising academic papers
- 📊 **Research Workflow Automation** - Template-driven processes for knowledge acquisition, citation management, and systematic reviews
- 🎯 **Confidence-Aware AI** - All AI-generated content includes confidence levels and uncertainty quantification

## 🚀 How to Start

### 1. **Prerequisites:**
- Python 3.10 or higher
- Node.js 16+ (for frontend)
- PowerShell 7.x (for Windows)

### 2. **Installation:**

```powershell
# Clone the repository
git clone https://github.com/Strong-AI-Lab/Von.git
cd Von

# Run automated setup in Windows PowerShell
# This runs setup_py.ps1 (pdm dependencies, python packages, etc.) and setup_js.ps1 (Javascript dependencies)
./setup_all.ps1

# Configure VSCode settings
./setup_all.ps1 -ConfigureVSCode

# Configure VSCode settings and specific Python version (e.g., 3.12)
./setup_all.ps1 -ConfigureVSCode -PythonVersion 3.12

```

**VSCode workspace policy:** Committed workspace settings in `.vscode/` are intentionally limited to portable testing defaults (pytest/jest and safe test DB settings). Keep personal editor preferences (for example command auto-approve lists, local Jira views, and machine-specific paths) in your **user** VSCode settings, not workspace settings.

If directly running `./setup_all.ps1` does not work, then run each setup script separately:

```powershell
# Run setup_py.ps1
# Runs setup_py.ps1, which automatically finds and uses the newest compatible Python 3.10–3.15 version available in your PATH
./setup_py.ps1 -ConfigureVSCode

# How to specify a specific Python version (e.g., 3.12)
./setup_py.ps1 -ConfigureVSCode -PythonVersion 3.12

# Then,
./setup_js.ps1
```

**For Linux/macOS users:**
```bash
# Run the bash setup script
./setup_all.sh
```

**Note**: The bash scripts (`setup_all.sh`, `run.sh`) provide basic functionality but lack some advanced features available in the PowerShell versions. See [docs/script_comparison_report.md](docs/script_comparison_report.md) for details.

### 3. **Database Setup**

Von uses MongoDB to store your knowledge base (concepts, entities, relationships).

**MongoDB Installation:**
MongoDB will be automatically installed during the setup process (via `setup_py.ps1`). If automatic installation fails or you prefer manual installation, download MongoDB Community Edition from https://www.mongodb.com/try/download/community

**Configuration:**

1. **Copy the template:** `cp .env.template .env` (or `.env.template` to `.env`)

2. **Choose your database option** by editing `.env`:

   - **Local MongoDB** (Default):
     ```bash
     MONGO_URI=mongodb://localhost:27017/
     VON_DB_NAME=von_db
     ```
     Make sure that MongoDB is running on your local machine.

   - **MongoDB Atlas** (Cloud - Optional for team collaboration):
     ```bash
     MONGO_URI=mongodb+srv://YOUR_USERNAME:YOUR_PASSWORD@YOUR_CLUSTER.mongodb.net/
     VON_DB_NAME=von_db
     MONGO_PROJECT=Your Project Name
     ```

you should see `[Von Database] Connecting to LOCAL MongoDB at localhost:27017` or connection to your Atlas cluster in log file under `./logs` folder once running the system.

**Starting with Knowledge:**

By default, you start with an empty knowledge base. You can:
- Create concepts manually through the Von interface
- Load sample ontology
- Build your custom ontology from scratch and import it

You have two options for initializing your knowledge base (note: SAIL members with access to remote SAIL database do not need to do this):

**Option 1: Manual Creation** (Empty Start)
- Start Von and create concepts through the user interface
- Build your ontology from scratch with full control
- Ideal for: Custom domains, specific research needs

**Option 2: Load Sample Base Knowledge** (Quick Start)
A sample base ontological knowledge with a few concepts and their definition is provided in `./sample_knowledge` folder.

To load the sample knowledge base:

```powershell
# Ensure you're in the Von root directory
# 1. Activate Python environment (if not already active)
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
# or: source .venv/bin/activate  # Mac/Linux

# 2. Make sure that your local database does not already have corrupted 'von_db' (drop 'von_db' if it exists)

# 3. Run the initialization script
python src/utilities/init_database.py --full-setup
```

**What this does:**
- Creates database collections and indexes
- Loads sample interconnected AI concepts with relationships
- Sets up proper ontology hierarchy
- Provides working examples for exploration

**After loading**, start Von normally:
```powershell
./run.ps1
```

Visit `http://localhost:5000` and explore the pre-loaded concepts in the interface. You can extend this base ontology by adding your own concepts and relationships.

### 4. **LLM Provider Setup**

**Ollama (Local Models):**
1. Install Ollama: https://ollama.ai
2. Pull models: `ollama pull llama3.2` (or your preferred model) or install directly from ollama UI.
3. Ensure Ollama is running: `ollama serve`
4. Von will auto-detect available models (if not, select the installed local model in Von's Settings panel)

**OpenAI:**
1. Get your own API key from https://platform.openai.com/api-keys
2. Set `OPENAI_API_KEY` in `.env`
3. Select OpenAI model in Von's Settings panel

**Google Gemini:**
1. Get your own API key from https://ai.google.dev
2. Set `GOOGLE_API_KEY` in `.env`
3. Select Gemini model in Von's Settings panel

### 5. **UI Feature Flags**

- `VON_EXPERT_TABS_ENABLED=1` enables the expert tabs (Vontology, Import/Export, Annotation) and annotation controls. Default behaviour is disabled unless set.
- `VON_EXPERT_FOOTER_ENABLED=1` enables the PID/RAG status footer and modal. Default behaviour is disabled unless set.

### 6. **Run:**

**Windows (PowerShell):**
```powershell
# Start the server locally
./run.ps1

# Open browser to http://localhost:5000
```

**Linux/macOS (Bash):**
```bash
# Start the server locally
./run.sh start

# Open browser to http://localhost:5000
```

### 7. **Stop:**

**Windows (PowerShell):**
```powershell
# Stop the local server
./run.ps1 stop
```

**Linux/macOS (Bash):**
```bash
# Stop the local server
./run.sh stop
```

### 8. **Basic Usage:**
-  Browse the Vontology tree to explore knowledge structure
-  Create your first concepts and entities, and make relations between them to construct your own ontological knowledge
-  Configure your preferred LLM provider in Settings (Ollama local models by default)
-  Start a chat conversation with Von's AI assistant

See **[User Guide](USER_GUIDE.md)** for comprehensive guide to Von's features and workflows *(coming soon)*

## 🎯 Quick Usage Examples

### **Ex 1. Systematic Literature Review**
Von helps researchers conduct comprehensive literature reviews by:
- Importing papers from arXiv and other sources
- Organising literature by concepts and relationships
- Tracking citations and research connections
- Using AI to identify research gaps and trends
- Generating confidence-assessed summaries

**Example Workflow:**
1. Define research concepts in Vontology (e.g., "Causal Reasoning", "Neural Networks")
2. Import relevant papers via arXiv integration
3. Annotate papers with salient predicates
4. Chat with Von to synthesise findings across papers
5. Export structured bibliography with relationships

### **Ex 2. Research Network Mapping**
Build comprehensive maps of academic relationships:
- Track researchers, institutions, and collaborations
- Map expertise areas and research domains
- Identify potential collaborators and research trends
- Understand institutional relationships

**Example Workflow:**
1. Create Person entities for key researchers
2. Link researchers to their scholarly works
3. Use Vontology to classify expertise areas
4. Visualise collaboration networks
5. Query Von for collaboration opportunities

### **Ex 3. Knowledge Base Construction**
Create structured, verifiable knowledge repositories:
- Build domain-specific ontologies extending Vontology
- Capture expert knowledge through AI-assisted interviews
- Track knowledge provenance and confidence
- Enable team-based knowledge curation
- Export knowledge in standard formats

**Example Workflow:**
1. Define domain concepts in Vontology hierarchy
2. Use knowledge acquisition workflows to gather information
3. Link entities to concepts with predicates
4. Validate knowledge with confidence assessment
5. Share knowledge base with research team

## 🤝 Contributing

We welcome contributions from the community! Von is designed to evolve with academic research needs.

**How to Contribute:**
1. Read our [Contributing Guidelines](CONTRIBUTING.md)
2. Browse [open issues](https://github.com/Strong-AI-Lab/Von/issues) or propose new features
3. Submit pull requests with tests and documentation

**Areas for Contribution (ex):**
- External MCP server integrations
- Automatic information extraction for entity creation
- Domain-specific Vontology extensions
- Research workflow templates
- Documentation improvements
- Frontend improvements

## 📄 Licence

Von is licensed under the **Apache Licence 2.0**. See [LICENSE](LICENSE) for details.

This means you can:
- ✅ Use Von commercially
- ✅ Modify and distribute
- ✅ Use Von in proprietary software

With the requirement to:
- 📝 Include copyright notice
- 📝 State significant changes
- 📝 Include Apache 2.0 licence text

## 🙏 Acknowledgements

Von is developed by the **Strong AI Lab** at the University of Auckland, with contributions from researchers and developers committed to advancing AI-assisted academic research.

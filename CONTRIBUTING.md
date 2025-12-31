# Contributing to Von

Thank you for your interest in contributing to **Von**! This AI-agent system for academic research is developed by the Strong AI Lab at the University of Auckland, and we welcome contributions from the community.

This guide will help you get started with contributing to the project. For an overview of Von's features and goals, see the [README](README.md).

---

## 🚀 Getting Started

### Prerequisites

Before you begin, ensure you have the following installed:

- **Python 3.10 or higher**
- **Node.js 16+** (for frontend development)
- **Git** for version control

### Fork and Clone

**For external contributors (recommended):**

If you don't have write access to the main repository, you'll need to fork it:

1. **Fork** the repository on GitHub by clicking the "Fork" button at https://github.com/Strong-AI-Lab/Von
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/Von.git
   cd Von
   ```
3. **Add upstream** remote to keep your fork in sync:
   ```bash
   git remote add upstream https://github.com/Strong-AI-Lab/Von.git
   ```

**For collaborators with write access:**

If you have write access to the repository, you can clone directly:

```bash
git clone https://github.com/Strong-AI-Lab/Von.git
cd Von
```

**Important:** If you cloned directly without forking and don't have write access, you won't be able to push branches. In this case:
1. Fork the repository on GitHub
2. Update your local repository's remote:
   ```bash
   # Rename the current origin to upstream
   git remote rename origin upstream

   # Add your fork as the new origin
   git remote add origin https://github.com/YOUR-USERNAME/Von.git

   # Verify remotes
   git remote -v
   ```

### Keeping Your Fork in Sync

If you're working from a fork, sync it regularly with upstream:

```bash
# Fetch latest changes from upstream
git fetch upstream

# Switch to your main branch
git checkout main

# Merge upstream changes
git merge upstream/main

# Push updates to your fork
git push origin main
```

### Setup Your Development Environment

Run the automated setup script in PowerShell:

```powershell
# Install Python and JavaScript dependencies
./setup_all.ps1

# If you need to specify a Python version (e.g., 3.12):
./setup_py.ps1 -ConfigureVSCode -PythonVersion 3.12
./setup_js.ps1
```

### Verify Your Setup

Start the development server:

```powershell
./run.ps1
```

Open your browser to `http://localhost:5001` and verify Von is running correctly.

To stop the server:

```powershell
./run.ps1 stop
```

---

## 🔐 Environment Variables and Secrets

Von uses environment variables for configuration (database, API keys, tokens).

- The repo-root `.env` file is for *local secrets* and is intentionally ignored by git (see `.gitignore`). Never commit it.
- Use `.env.template` as the source of truth for documented configuration options. If your change introduces a new environment variable, add a commented placeholder and brief explanation to `.env.template`.
- Avoid pasting secrets into issues, pull requests, logs, or screenshots.

---

## 🔄 Development Workflow

### 1. Create a Branch

Before making changes, create a new branch from `main`:

**If working from a fork:**
```bash
git checkout main
git pull upstream main  # Sync with upstream
git checkout -b feature/your-feature-name
```

**If working with direct clone (write access):**
```bash
git checkout main
git pull origin main  # Sync with main repo
git checkout -b feature/your-feature-name
```

**Branch naming convention:**
- `feature/` - New features or enhancements
- `fix/` - Bug fixes
- `refactor/` - Code refactoring
- Etc.

Examples:
- `feature/arxiv-integration`
- `fix/concept-search-bug`

### 2. Make Your Changes and Test

Make your changes following the project conventions. See the [Suggested Style](#-suggested-style) section for code style guidelines.

Before committing, ensure your changes work correctly by running the application locally with `./run.ps1` and testing your modifications.

### 3. Commit Your Changes

Write clear, concise commit messages that describe what changed and why:

```bash
git add .
git commit -m "Add arXiv paper import feature to concept service"
```

**Good commit messages:**
- ✅ "Fix concept search returning duplicate results"
- ✅ "Add support for Google Gemini API integration"
- ✅ "Refactor annotation extraction to improve performance"
- ✅ "Update README with Ollama setup instructions"

### 6. Push and Create a Pull Request

Push your branch to the appropriate remote:

**If working from a fork:**
```bash
git push origin feature/your-feature-name
```

**If working with direct clone (write access):**
```bash
git push origin feature/your-feature-name
```

Then open a **Pull Request (PR)** on GitHub:

**For fork contributors:**
1. Go to your fork on GitHub: `https://github.com/YOUR-USERNAME/Von`
2. GitHub will show a prompt to "Compare & pull request" for your recently pushed branch
3. Click it, or manually go to the [main Von repository](https://github.com/Strong-AI-Lab/Von) and click "New Pull Request"
4. Select "compare across forks" and choose your fork and branch
5. **Write a detailed description** (see template below)

**For direct collaborators:**
1. Go to the [Von repository](https://github.com/Strong-AI-Lab/Von)
2. Click **"New Pull Request"**
3. Select your branch from the dropdown
4. **Write a detailed description** (see template below)

**PR Description should include:**
- **What** changes were made
- **Why** the changes were necessary
- Reference any related issues (e.g., "Fixes #123")

**Example PR description:**

```markdown
## Description
Adds integration with the arXiv API to allow users to import papers directly into their knowledge base.

## Changes
- Added `arxiv_client.py` with search and fetch methods
- Created new route `/api/papers/import-arxiv`
- Updated concept service to link papers to concepts
- Added tests for arXiv integration

```

### 7. PR Review Process

- **Required approvals:** Your PR will need approval from project maintainers before merging
- **Be responsive:** Address review comments and feedback promptly
- **Iterate:** Push additional commits to the same branch to update your PR
- **Stay patient:** Reviews may take a few days depending on maintainer availability

---

## 🎯 Areas for Contribution (Ex)

We particularly welcome contributions in these areas:

### 1. **MCP Server Integrations**
- Integrate external APIs (e.g., Semantic Scholar, PubMed, Google Scholar)
- Extend existing MCP servers with new capabilities
- Create domain-specific MCP tools

### 2. **Entity Extraction & Automation**
- Improve automatic extraction of researchers, institutions, concepts from text
- Build pipelines for bulk entity creation
- Enhance NLP-based annotation systems

### 3. **Vontology Extensions**
- Add domain-specific ontology branches (biology, computer science, social sciences)
- Define new relationship types and predicates
- Create validation rules for ontology consistency

### 4. **Research Workflow Templates**
- Systematic literature review workflows
- Citation network analysis tools
- Collaboration mapping features

### 5. **Documentation**
- User guides and tutorials
- API documentation
- Code examples and use cases

### 6. **Frontend Improvements**
- UI/UX enhancements
- Graphical UI improvements
- Data visualization features
- Accessibility improvements

---

## 📖 Suggested Style

When contributing, please follow these guidelines:

- **Code comments:** Explain complex logic and algorithms
- **Docstrings:** Add docstrings to Python functions and classes (Google style preferred)
- **Type hints:** Use Python type hints for better code clarity

**Python docstring example:**

```python
def create_concept(name: str, parent_id: str, description: str = "") -> dict:
    """
    Create a new concept in the Vontology hierarchy.

    Args:
        name: The name of the concept
        parent_id: The ID of the parent concept
        description: Optional description of the concept

    Returns:
        A dictionary with 'success' boolean and 'concept' data

    Raises:
        ValueError: If parent_id is invalid
    """
    # Implementation...
```

---

## 💬 Communication & Questions

### GitHub Issues

- **Bug reports:** Use the issue tracker to report bugs with reproducible steps
- **Feature requests:** Propose new features with clear use cases
- **Questions:** Ask questions about the codebase or development process

### Issue Template

When creating an issue, include:
- **Description:** Clear summary of the issue or request
- **Steps to reproduce:** For bugs, provide detailed steps
- **Expected behavior:** What should happen
- **Actual behavior:** What actually happens
- **Environment:** OS, Python version, browser (if applicable)
- **Screenshots:** Visual evidence if relevant

---

## 🤝 Code of Conduct

We are committed to providing a welcoming and inclusive environment for all contributors. We expect:

- **Respectful communication:** Treat everyone with respect and courtesy
- **Constructive feedback:** Provide helpful, actionable feedback in reviews
- **Open-mindedness:** Be open to different approaches and perspectives
- **Collaboration:** Work together to build the best possible solution
- **Academic integrity:** Maintain high standards of honesty and attribution

Unacceptable behavior includes harassment, discriminatory language, or disruptive conduct. If you experience or witness unacceptable behavior, please contact the maintainers.

---

## 📄 License

By contributing to Von, you agree that your contributions will be licensed under the **Apache License 2.0**, the same license as the project. See [LICENSE](LICENSE) for details.

---

## 🙏 Thank You!

Your contributions help make Von a better tool for academic researchers worldwide. Whether you're fixing a typo, adding a feature, or improving documentation—every contribution matters!

If you have questions or need help, don't hesitate to open an issue or reach out to the maintainers.

**Happy Contributing! 🚀**

---

**Strong AI Lab (SAIL)**
University of Auckland
https://github.com/Strong-AI-Lab

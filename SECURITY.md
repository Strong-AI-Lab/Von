# Security Policy

## Reporting Security Vulnerabilities

The Von project takes security seriously. We appreciate your efforts to responsibly disclose any security vulnerabilities you may find.

### How to Report a Vulnerability

**Please DO NOT open a public issue for security vulnerabilities.**

Instead, please report security vulnerabilities by:

1. **Email:** Contact the project maintainers directly
2. **GitHub Security Advisories:** Use the [GitHub Security Advisory](https://github.com/Strong-AI-Lab/Von/security/advisories/new) feature (preferred method)

Please include the following information in your report:
- Description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Any suggested fixes (if available)

### What to Expect

- **Acknowledgement:** We will acknowledge receipt of your vulnerability report within 48 hours
- **Updates:** We will provide regular updates on our progress
- **Timeline:** We aim to address critical vulnerabilities within 7-14 days
- **Credit:** We will credit you in our security advisories (unless you prefer to remain anonymous)

## Security Measures

### Credential Management

Von uses environment variables for all sensitive configuration:

- **API Keys:** OpenAI, Google Gemini, etc. via environment variables
- **Database Credentials:** MongoDB connection strings via `MONGO_URI`
- **OAuth Secrets:** Google OAuth client secrets via environment variables
- **Tokens:** Admin tokens generated at runtime, not committed to repository

### Secret Scanning

The repository is protected against accidental secret exposure:

1. **Automated Scanning:** GitHub Actions workflow using Gitleaks
2. **Pre-commit Prevention:** Sensitive files excluded via `.gitignore`
3. **Regular Audits:** Periodic security scans of codebase and dependencies

### Environment Variable Configuration

All credentials must be configured using environment variables or `.env` files (which are excluded from version control).

**Required Environment Variables:**
```bash
# Database
MONGO_URI=your_mongodb_connection_string

# LLM Providers (at least one required)
OPENAI_API_KEY=your_openai_key
GOOGLE_API_KEY=your_google_key

# OAuth (if using Google authentication)
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret

# Atlassian/JIRA (optional)
ATLASSIAN_SITE_BASE=your_site_base
ATLASSIAN_API_EMAIL=your_email
ATLASSIAN_API_TOKEN=your_token
```

See `.env.template` for a complete example configuration.

## Secure Development Practices

### For Contributors

1. **Never Commit Secrets:** Do not commit API keys, passwords, or tokens
2. **Use Environment Variables:** Always load credentials from environment
3. **Review Before Pushing:** Check your commits for sensitive data
4. **Update Dependencies:** Keep dependencies up to date with security patches

### Code Review

All pull requests undergo security review:
- Credential handling verification
- Input validation checks
- Dependency vulnerability scanning
- Secret scanning via automated tools

## Supported Versions

We provide security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| main    | :white_check_mark: |
| develop | :white_check_mark: |
| < 1.0   | :x:                |

## Security Audit History

- **2025-11-16:** Initial comprehensive secret scan - No vulnerabilities found
  - Tool: Gitleaks v8.18.4
  - Scope: Full repository and git history
  - Status: ✅ Clean
  - Report: [SECURITY_SCAN_REPORT.md](SECURITY_SCAN_REPORT.md)

## Security Best Practices for Users

### Setting Up Securely

1. **Copy the template:** `cp .env.template .env`
2. **Add your credentials:** Edit `.env` with your actual values
3. **Verify exclusion:** Ensure `.env` is in `.gitignore`
4. **Restrict permissions:** `chmod 600 .env` (Unix/Linux/macOS)

### MongoDB Security

- Use strong connection strings
- Enable MongoDB authentication
- Use TLS/SSL for remote connections
- Regularly rotate credentials
- Restrict network access to MongoDB

### API Key Security

- Never share API keys publicly
- Rotate keys regularly
- Use separate keys for development and production
- Monitor API usage for suspicious activity
- Revoke compromised keys immediately

## Contact

For security-related questions or concerns, please contact the project maintainers through:
- GitHub Security Advisories (preferred)
- Project issue tracker (for non-sensitive questions only)

---

**Last Updated:** 2025-11-16  
**Security Contact:** Via GitHub Security Advisories

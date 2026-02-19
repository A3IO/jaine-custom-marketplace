---
name: warn-sensitive-files
enabled: true
event: file
action: warn
additionalContext: "SENSITIVE FILE EDIT detected. Before writing: (1) verify file is in .gitignore, (2) never hardcode credentials — use env vars, (3) if adding API keys, use placeholder values like YOUR_API_KEY_HERE."
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.env$|\.env\.|credentials|secrets
---

🔐 **Sensitive file detected**

You're editing a file that may contain sensitive data:
- Ensure credentials are not hardcoded
- Use environment variables for secrets
- Verify this file is in .gitignore
- Consider using a secrets manager

# Security and Sensitive Files

## Never read or expose secrets

The following files are sensitive:

.env
.env.*
.axiom/config.json
credentials.json
*.key
*.pem
*.secret
*token*

Do not:

- read their contents;
- print their contents;
- include them in reports;
- copy them into generated documents;
- commit them to git.

If configuration verification is needed:

Only report:

configured / missing

Never output actual values.

---

## API keys

Sensitive environment variables include:

DEEPSEEK_API_KEY
AXIOM_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY

Never execute commands that reveal values:

echo $VARIABLE
printenv
Get-Content .env
cat .env

Only check existence.

---

## Local configuration

Files under:

.axiom/

may contain:

- user configuration;
- credentials;
- memory;
- local state.

Do not inspect contents unless explicitly requested.

---

## Git safety

Before git operations:

Check:

- .gitignore exists;
- secrets are excluded.

Never:

- git add .env;
- commit secrets;
- upload credentials.

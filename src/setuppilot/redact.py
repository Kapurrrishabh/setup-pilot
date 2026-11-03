"""Hide secrets in any text before it is logged or shown."""

import json
import os
import re

SECRET_ENV_NAMES = ("GROQ_API_KEY", "SUPABASE_DB_URL", "LANGSMITH_API_KEY")
MASK = "***"

# Catch secrets that are not in our env, e.g. a key printed by a repo's install script.
_SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"lsv2_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    # Anything before @ in a URL (user:password, or a token) is a secret.
    re.compile(r"(?<=://)[^/\s]{1,256}(?=@)"),
)
# Shorter values are placeholders; hiding them would garble every log line.
_MIN_SECRET_LENGTH = 8


def redact(text: str) -> str:
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name, "")
        if len(value) >= _MIN_SECRET_LENGTH:
            # Log lines are JSON, so hide the escaped form first, then the raw one.
            for form in (json.dumps(value)[1:-1], value):
                text = text.replace(form, MASK)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    return text

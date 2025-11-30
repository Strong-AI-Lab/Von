import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

# Mimic the loading logic in mongo_client.py
try:
    _this_file = Path("src/backend/db/mongo_client.py").resolve()
    # This path calculation might be slightly different depending on where I run this script from
    # Let's just use the standard load_dotenv
    load_dotenv(override=False)
except Exception:
    pass

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_ALLOW_LOCAL_FALLBACK = os.environ.get("MONGO_ALLOW_LOCAL_FALLBACK", "1")
MONGO_ALLOW_LOCAL_FALLBACK_BOOL = MONGO_ALLOW_LOCAL_FALLBACK in ("1", "true", "True")

print(f"MONGO_URI: {MONGO_URI}")
print(f"MONGO_ALLOW_LOCAL_FALLBACK (raw): {MONGO_ALLOW_LOCAL_FALLBACK}")
print(f"MONGO_ALLOW_LOCAL_FALLBACK (bool): {MONGO_ALLOW_LOCAL_FALLBACK_BOOL}")

# Simulate the error check
e_str = "SSL handshake failed: ac-mi1dnlr-shard-00-01.3nnlvtl.mongodb.net:27017: [WinError 10054]"
is_ssl_error = "SSL" in e_str or "10054" in e_str or "handshake" in e_str.lower()
print(f"is_ssl_error: {is_ssl_error}")

should_try_fallback = MONGO_ALLOW_LOCAL_FALLBACK_BOOL and (
    MONGO_URI.startswith("mongodb+srv://") or is_ssl_error
)
print(f"should_try_fallback: {should_try_fallback}")

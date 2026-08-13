from datetime import datetime, timezone
from uuid import uuid4

def new_id() -> str: return str(uuid4())
def new_case_no() -> str:
    return f"VOIP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"

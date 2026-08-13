import json, logging, sys
from datetime import datetime, timezone

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("case_id","job_id","device_id","trace_id"):
            value = getattr(record, key, None)
            if value: payload[key]=value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def configure_logging(level="INFO"):
    handler=logging.StreamHandler(sys.stdout); handler.setFormatter(JsonFormatter())
    root=logging.getLogger(); root.handlers=[handler]; root.setLevel(level)

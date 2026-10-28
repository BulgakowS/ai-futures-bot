import json
import time
from urllib.error import HTTPError
from urllib.request import urlopen

for attempt in range(30):
    try:
        with urlopen("http://127.0.0.1:8000/api/v1/health/ready", timeout=8) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
        with urlopen("http://127.0.0.1:8000/api/v1/state", timeout=3) as response:
            state = json.load(response)
            assert state["mode"] == "paper" and state["kill_switch"] is True
            print(json.dumps(state, ensure_ascii=False))
        break
    except HTTPError as error:
        if error.code != 503 or attempt == 29:
            raise
        time.sleep(1)
else:
    raise SystemExit("Readiness failed")

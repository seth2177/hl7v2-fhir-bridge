"""python -m mock_fhir [--port 8080]  -- run the mock FHIR server in the foreground."""
import argparse
import time

from .server import MockFhirServer

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8080)
a = ap.parse_args()
srv = MockFhirServer(a.host, a.port).start()
print(f"mock FHIR server at {srv.base_url}  (Ctrl+C to stop)")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    srv.stop()

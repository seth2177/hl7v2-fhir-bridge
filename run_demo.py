"""One command, whole workflow. The demo lives in v2fhir/demo.py; installed, it is `hl7v2-fhir-bridge demo`.

  python run_demo.py                                  # mock FHIR server, bundles in ./data/bundles
  python run_demo.py --fhir-url http://localhost:8080/fhir   # a real server instead (e.g. HAPI FHIR)
"""
from v2fhir.demo import main

if __name__ == "__main__":
    main()

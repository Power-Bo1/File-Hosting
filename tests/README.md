# Tests

Run from the project root:

    python3 -m unittest discover -s tests -v        # stdlib runner
    # or, after: pip install -r api/requirements.txt -r web/requirements.txt -r requirements-dev.txt
    pytest tests/ -v

- test_security.py - token + filename + credential rules (needs PyJWT only)
- test_web.py      - Flask frontend with the API stubbed (needs Flask only)
- test_api.py      - full API endpoints over a fake DB (needs fastapi/httpx/
                     bcrypt/psycopg2; auto-skips when missing)
- scripts/validate_k8s.py - static manifest cross-checks (needs PyYAML)

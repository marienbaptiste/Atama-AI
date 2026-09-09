"""SRS integrations (WaniKani, Bunpro). READ-ONLY — spec §0 Golden Rule, ADR-021.

Every module here goes through `backend.srs.http.SrsClient.get`. Nothing else.
`backend/tools/readonly_gate.py` verifies that at every test/run/doctor/commit.
"""

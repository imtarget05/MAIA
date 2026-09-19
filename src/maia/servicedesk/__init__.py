"""MAIA Service Desk — standalone IT service desk built on MAIA.

Scope: plan-20260919-1820-maia-service-desk-v1 (see plans/).
Isolation rules: this package must never import from the project 1 donor
checkout at runtime, and must not mount legacy MAIA chat/tool routes in
its public API (see T1+). Donor provenance is tracked only in
docs/service-desk/reuse-manifest.json.
"""

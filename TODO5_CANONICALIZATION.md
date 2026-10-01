# TODO 5 Canonical Verification

STATUS: VERIFIED DONE

Branch: todo5-reconcile (based on origin/main @ 8faabf02)
Decision: Decision A - require-ID UAMI contract maintained, CI syntax reconciled

Local evidence:
- 36 passed, 1 skipped (identity + negative control tests)
- IdentityConfigurationError: require-UAMI-ID on Azure
- Config validation before optional SDK import
- SecretClient caching per vault URI
- Two secret lists documented (KV_SECRET_NAMES vs keyVaultSecretNames)
- Negative controls NC1-NC10 intact

Canonical SHA: 8faabf02

FILES MODIFIED (from reconcile):
- src/maia/azure_identity.py - IdentityConfigurationError, SDK ordering, SecretClient caching
- tests/test_azure_identity.py - tenant-alone test, Bicep-sync documented skip
- tests/test_identity_negative_controls.py - 5 NC tests
- .github/workflows/ci.yml - merged bicep-validate job

CI STATUS: pending push → PR → merge → main CI verification

// NEGATIVE FIXTURE — NOT DEPLOYED, NOT COMPILED.
//
// This file is test data for infra/validate.sh step 5b. It lives under
// validate/, which the compile step skips and the main secret scan excludes,
// precisely so a file containing real secrets can exist in the repository
// without turning the suite permanently red.
//
// WHY THIS EXISTS. The secret detector used `grep -rnE`, which is
// case-SENSITIVE. It matched `password = 'x'` and missed
// `DEMO_USER_A_PASSWORD = 'x'` -- but uppercase is the convention this repo
// and Azure use for every secret variable: DEMO_USER_A_PASSWORD,
// JWT_SECRET_KEY, QDRANT_API_KEY. The detector was green while blind to the
// exact shape of secret this project commits.
//
// Step 5b re-runs the SAME detector over this directory and REQUIRES it to
// fire. If someone reintroduces the case sensitivity, this check fails.
//
// The values below are deliberately fake and never used anywhere.

param demoUserPassword = 'Sup3rSecret-P@ssw0rd-not-a-real-credential'
param JWT_SECRET_KEY = 'jwt-signing-key-not-a-real-credential'
param QDRANT_API_KEY = 'qdrant-key-not-a-real-credential'
param dbPassword = 'lower-case-still-must-be-caught'
param clientSecret = 'sso-client-secret-not-a-real-credential'
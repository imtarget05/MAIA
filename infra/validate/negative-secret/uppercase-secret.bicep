// NEGATIVE FIXTURE — NOT DEPLOYED, NOT COMPILED.
//
// Test data for infra/validate.sh step 6. It lives under validate/, which the
// compile step skips and the main secret scan excludes, precisely so a file
// containing real-looking secrets can exist without turning the suite red.
//
// WHY THIS EXISTS. The secret detector ran `grep -rnE`, which is
// case-SENSITIVE: it matched `password = 'x'` and missed
// `DEMO_USER_A_PASSWORD = 'x'` -- uppercase being the convention this repo and
// Azure use for every secret variable. The detector was green while blind to
// the exact shape of secret this project commits.
//
// Step 6 re-runs the SAME detector here and REQUIRES it to fire. If case
// sensitivity ever returns, the suite fails instead of quietly going blind.
//
// All values below are fabricated and used nowhere.

param DEMO_USER_A_PASSWORD = 'not-a-real-demo-password'
param JWT_SECRET_KEY = 'not-a-real-jwt-signing-key'
param QDRANT_API_KEY = 'not-a-real-qdrant-key'
param dbPassword = 'lower-case-still-must-be-caught'
param clientSecret = 'not-a-real-client-secret'
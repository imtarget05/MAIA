#!/usr/bin/env python3
"""The delivery identity contract: what GitHub asserts must match what Azure accepts.

This is not a lint. It is a set of claims about a relationship between two files
that would otherwise drift apart silently:

    infra/oidc/expected-claims.json          what Azure will be configured to accept
    .github/workflows/delivery-identity.yml  what GitHub will actually assert

The federated credential created in Azure binds ONE subject string. If the
workflow's environment is renamed and the claims file is not, the credential
stops matching the only thing that can ever use it. Nothing fails at commit
time: the build is green, the deployment job is manual, and the break surfaces
weeks later as an authentication error that reads like a bad role assignment.

So the subject is not read from the claims file and compared to itself. It is
RECOMPUTED from the workflow's own environment, and the claims file is required
to agree. Neither file can be right on its own.

Claim validation is structured rather than string-equal, because the failure
being guarded against is not a typo -- it is a token from the wrong repository,
issued to the wrong audience, by the wrong issuer, arriving with no subject.
Those are different claims with different consequences, and a substring check
cannot tell them apart.

Run: python3 infra/scripts/test_oidc_contract.py
Exit 0 only if every contract holds.
"""

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CLAIMS = REPO / "infra/oidc/expected-claims.json"
WORKFLOW = REPO / ".github/workflows/delivery-identity.yml"
WORKFLOWS_DIR = REPO / ".github/workflows"

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
AZURE_AUDIENCE = "api://AzureADTokenExchange"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def workflow_permissions(wf):
    """The top-level permissions block, as text.

    PyYAML is not guaranteed on every CI runner for an infra-only check, and a
    control that fails to import is a control that silently passes elsewhere.
    The workflow is read as text and the few facts under test are extracted
    with narrow patterns. This is not a general YAML parser.
    """
    m = re.search(r"^permissions:\s*$((?:\n[ \t]+\S.*)+)", wf, re.MULTILINE)
    return m.group(1) if m else ""


def workflow_environments(wf):
    return re.findall(r"^\s*environment:\s*([A-Za-z0-9._-]+)\s*$", wf, re.MULTILINE)


wf = (WORKFLOW).read_text()
claims = json.loads(CLAIMS.read_text())
perms = workflow_permissions(wf)
environments = workflow_environments(wf)
expected_env = claims["environment"]

# --- 1. the workflow actually requests an OIDC token -------------------------
# NC-OIDC-1. GitHub silently refuses to mint a token when id-token is absent,
# so this is not ceremony: it is the difference between a workflow that can
# authenticate and one that cannot.

check("id-token: write present in workflow permissions",
      re.search(r"^\s*id-token:\s*write\s*$", perms, re.MULTILINE),
      "no id-token: write under permissions: -- GitHub will not mint an OIDC token")
check("contents permission is read-only",
      re.search(r"^\s*contents:\s*read\s*$", perms, re.MULTILINE),
      "contents should be read, not write")
check("no permission beyond contents:read and id-token:write",
      all(
          line.strip().split(":")[0].strip() in {"contents", "id-token"}
          for line in perms.splitlines()
          if line.strip() and not line.strip().startswith("#")
      ),
      "an unexpected permission was granted")

# --- 2. workflow and claims file agree ---------------------------------------
# NC-OIDC-2 and NC-OIDC-6. The subject is recomputed from the workflow and
# required to equal the claims file. This is the check that would catch an
# environment rename, the drift that leaves a credential unusable.

check("claims file names exactly one environment",
      isinstance(expected_env, str) and bool(expected_env),
      f"environment must be a single non-empty string, got {expected_env!r}")
check("workflow declares the environment from the claims file",
      expected_env in environments,
      f"claims say {expected_env!r} but workflow declares {environments}")
check("workflow declares no other environment than the claims one",
      set(environments) <= {expected_env},
      f"workflow declares {environments}; only {expected_env!r} is authorised")

recomputed = f"repo:{claims['repository']}:environment:{expected_env}"
check("subject recomputed from workflow matches the claims file",
      recomputed == claims["subject"],
      f"workflow implies {recomputed!r} but claims assert {claims['subject']!r}")
check("claims subject is environment-scoped, not branch-scoped",
      ":environment:" in claims["subject"],
      "a branch subject keeps working for workflow_dispatch from any ref")

# --- 3. issuer and audience --------------------------------------------------

check("issuer is the GitHub Actions issuer",
      claims["issuer"] == GITHUB_ISSUER,
      f"issuer {claims['issuer']!r} is not {GITHUB_ISSUER}")
check("audience is the Azure token exchange audience",
      claims["audience"] == AZURE_AUDIENCE,
      f"audience {claims['audience']!r} would be rejected by Entra")

# --- 4. structured claim validation ------------------------------------------
# Each case is a token GitHub could plausibly present that Azure must refuse.
# They are checked separately because they fail for different reasons: a
# repository mismatch is a trust boundary error, a wrong audience is a token
# intended for something else, and a missing subject is a token that proves
# nothing at all. Collapsing them into one "claims differ" check would pass a
# validator that only ever compared issuer.


def accepts(token):
    """Model the rule Entra applies to an external-issuer token."""
    return (
        token.get("iss") == claims["issuer"]
        and token.get("aud") == claims["audience"]
        and token.get("sub") == claims["subject"]
        and token.get("repository") == claims["repository"]
    )


good = {
    "iss": claims["issuer"],
    "aud": claims["audience"],
    "sub": claims["subject"],
    "repository": claims["repository"],
}

check("ACCEPT a token from the right repo, environment, issuer and audience",
      accepts(good))
check("REJECT a token from another repository",
      not accepts(dict(good, repository="imtarget05/some-other-repo",
                       sub=f"repo:imtarget05/some-other-repo:environment:{expected_env}")))
check("REJECT a token from another environment",
      not accepts(dict(good, sub=f"repo:{claims['repository']}:environment:staging")))
check("REJECT a branch subject where an environment is configured",
      not accepts(dict(good, sub=f"repo:{claims['repository']}:ref:refs/heads/main")))
check("REJECT a token with the wrong issuer",
      not accepts(dict(good, iss="https://accounts.google.com")))
check("REJECT a token for a different audience",
      not accepts(dict(good, aud="https://management.azure.com/")))
check("REJECT a token with no subject",
      not accepts(dict(good, sub="")))
check("REJECT a token with no issuer",
      not accepts(dict(good, iss="")))
check("REJECT a subject naming another repository but the right environment",
      not accepts(dict(good, sub=f"repo:attacker/repo:environment:{expected_env}")))

# --- 5. the subject may not be a wildcard ------------------------------------
# A wildcard turns a credential scoped to one environment into one any workflow
# in the org can use, which is the opposite of the point.

check("subject names no wildcard",
      "*" not in claims["subject"],
      f"wildcard subject {claims['subject']!r} is not a boundary")
check("subject names exactly one repository",
      claims["subject"].count("repo:") == 1,
      f"subject {claims['subject']!r} names more than one repository")
check("repository is owner/name",
      re.fullmatch(r"[\w.-]+/[\w.-]+", claims["repository"]),
      f"repository {claims['repository']!r} is not owner/name")

# --- 6. no long-lived Azure credential anywhere -------------------------------
# NC-OIDC-5. Scanned across every workflow, not just this one, because the
# regression being guarded against is someone adding a secret to a *different*
# workflow to work around this one failing.

BANNED = {
    "AZURE_CLIENT_SECRET": "service principal password",
    "AZURE_CLIENT_CERTIFICATE": "certificate-based credential",
    "AZURE_CLIENT_CERTIFICATE_PATH": "certificate-based credential",
    "AZURE_AD_RESOURCE": "SPN login (az login --service-principal)",
}

for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
    text = path.read_text()
    for key, what in BANNED.items():
        check(f"no {key} in {path.name} ({what})",
              key not in text,
              f"{path.name} references {key}")
    check(f"no service-principal login in {path.name}",
          not re.search(r"--service-principal|az login --username", text),
          f"{path.name} authenticates with a service principal")

# A secret can also arrive under a name not in that list, so the login step is
# additionally checked structurally: azure/login must receive exactly the three
# public identifiers and nothing else.

deploy_login = re.search(
    r"uses:\s*azure/login@v\d+\s*\n((?:\s+\S.*\n)+?)\s*run:", wf)
if deploy_login:
    provided = set(re.findall(r"^\s{6,}([a-z-]+):", deploy_login.group(1), re.MULTILINE))
    # `with:` is the wrapper key azure/login takes its inputs under, not an
    # input itself. Counting it would make the check reject every correct
    # workflow, which is how a control gets switched off.
    provided.discard("with")
    expected_inputs = {"client-id", "tenant-id", "subscription-id"}
    check("azure/login is given only public identifiers",
          provided <= expected_inputs,
          f"unexpected azure/login inputs: {sorted(provided - expected_inputs)}")
    check("azure/login does not receive a secret",
          not any("secret" in p or "password" in p for p in provided),
          "azure/login must not receive a secret")
else:
    check("azure/login step found for input inspection", False,
          "no azure/login step found to verify its inputs")

# --- 7. the workflow does not claim more than it proves -----------------------

check("claims file records that the credential is not yet created",
      "NOT CREATED" in claims.get("_credential_state", ""),
      "claims file must not imply a live federated credential exists")
check("azure deploy job is gated behind workflow_dispatch",
      re.search(r"azure-deploy:[\s\S]{0,300}?if:\s*github\.event_name\s*==\s*"
                r"'workflow_dispatch'", wf),
      "the Azure job must not run unattended before the credential exists")

# --- verdict -----------------------------------------------------------------

passed = sum(1 for _, ok, _ in results if ok)

for name, ok, detail in results:
    if not ok:
        print(f"FAIL  {name}")
        if detail:
            print(f"        {detail}")

print(f"\n{passed}/{len(results)} delivery identity contracts hold")

if passed != len(results):
    print("\nA delivery contract is wrong. Azure will either reject the token or")
    print("accept it from something it should not. Fix the contract rather than")
    print("the check -- a failing control here is the only signal that the")
    print("federated credential and the workflow have drifted apart.")
    sys.exit(1)

sys.exit(0)
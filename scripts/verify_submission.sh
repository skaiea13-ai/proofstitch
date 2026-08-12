#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/.." && pwd)"
cd "${repo_dir}"

uv sync --frozen --group dev --extra lint --no-install-project --no-build
uv run --no-sync pytest -q
uv run --no-sync ruff check app tests
uv run --no-sync ty check
uv run --no-sync codespell README.md docs app tests scripts
xmllint --noout docs/architecture.svg
git diff --check
gitleaks dir . --no-banner --redact --exit-code 1
gitleaks git . --no-banner --redact --exit-code 1

if [[ "${1:-}" == "--docker" ]]; then
  docker build -t proofstitch-agent:verify .
  proof_container_name="proofstitch-agent-verify-$RANDOM"
  docker run --rm -d \
    --name "${proof_container_name}" \
    -p 127.0.0.1::8080 \
    proofstitch-agent:verify >/dev/null
  trap 'docker stop "${proof_container_name}" >/dev/null 2>&1 || true' EXIT
  proof_port="$(docker port "${proof_container_name}" 8080/tcp | sed 's/.*://')"
  for _ in {1..20}; do
    if curl --fail --silent "http://127.0.0.1:${proof_port}/healthz" >/dev/null; then
      break
    fi
    sleep 1
  done
  curl --fail --silent "http://127.0.0.1:${proof_port}/healthz" >/dev/null
  workflow_json="$(curl --fail --silent -X POST \
    -H 'X-ProofStitch-Workflow-Intent: fixed-synthetic-workflow' \
    "http://127.0.0.1:${proof_port}/api/v1/workflows/demo")"
  jq -e '
    .report.status == "AWAITING_APPROVAL" and
    (.actions | length) == 6 and
    .external_action_executed == false
  ' <<<"${workflow_json}" >/dev/null
fi

echo "ProofStitch submission verification passed."

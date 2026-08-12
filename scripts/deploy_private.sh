#!/usr/bin/env bash
set -euo pipefail

service_name="proofstitch"
cleanup_queue="proofstitch-cleanup"
cleanup_service_account_id="proofstitch-cleanup"
max_demo_window_seconds=$((10 * 60))
cleanup_poll_attempts="${PROOFSTITCH_CLEANUP_POLL_ATTEMPTS:-30}"
cleanup_poll_interval_seconds="${PROOFSTITCH_CLEANUP_POLL_INTERVAL_SECONDS:-2}"
iam_poll_attempts="${PROOFSTITCH_IAM_POLL_ATTEMPTS:-12}"
iam_poll_interval_seconds="${PROOFSTITCH_IAM_POLL_INTERVAL_SECONDS:-5}"

: "${PROOFSTITCH_PROJECT_ID:?Set the dedicated Google Cloud project ID.}"
: "${PROOFSTITCH_REGION:?Set the Cloud Run region.}"
: "${PROOFSTITCH_TASKS_LOCATION:?Set the Cloud Tasks location.}"
: "${PROOFSTITCH_SERVICE_ACCOUNT:?Set the least-privileged runtime service account.}"
: "${PROOFSTITCH_MODEL_DEMO_TOKEN:?Set a fresh 64-character lowercase hexadecimal model demo token.}"

model_demo_token="${PROOFSTITCH_MODEL_DEMO_TOKEN}"
unset PROOFSTITCH_MODEL_DEMO_TOKEN

if [[ "${PROOFSTITCH_DEPLOY_CONFIRM:-}" != "DEPLOY_PRIVATE" ]]; then
  echo "Refusing deployment: set PROOFSTITCH_DEPLOY_CONFIRM=DEPLOY_PRIVATE." >&2
  exit 2
fi
if [[ "${PROOFSTITCH_NO_COST_CONFIRMED:-}" != "YES" ]]; then
  echo "Refusing deployment: sponsored credit or free-tier coverage is not confirmed." >&2
  exit 2
fi
if [[ ! "${model_demo_token}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Refusing deployment: the model demo token must be 64 lowercase hexadecimal characters." >&2
  exit 2
fi
if [[ ! "${PROOFSTITCH_PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "Refusing deployment: the Google Cloud project ID is invalid." >&2
  exit 2
fi
if [[ ! "${PROOFSTITCH_REGION}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] ||
  [[ ! "${PROOFSTITCH_TASKS_LOCATION}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Refusing deployment: the Cloud Run region or Cloud Tasks location is invalid." >&2
  exit 2
fi
if [[ ! "${cleanup_poll_attempts}" =~ ^[1-9][0-9]*$ ]] ||
  [[ ! "${cleanup_poll_interval_seconds}" =~ ^[0-9]+$ ]] ||
  [[ ! "${iam_poll_attempts}" =~ ^[1-9][0-9]*$ ]] ||
  [[ ! "${iam_poll_interval_seconds}" =~ ^[0-9]+$ ]]; then
  echo "Refusing deployment: poll attempts and intervals must be non-negative integers." >&2
  exit 2
fi
for required_command in gcloud openssl jq curl; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Refusing deployment: ${required_command} is required." >&2
    exit 2
  fi
done
if ! CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
  gcloud beta policy-intelligence troubleshoot-policy iam --help \
  >/dev/null 2>&1; then
  echo "Refusing deployment: install the official Google Cloud CLI beta component for effective IAM checks." >&2
  echo "Run: gcloud components install beta" >&2
  exit 2
fi

model_demo_token_sha256="$(
  printf '%s' "${model_demo_token}" |
    openssl dgst -sha256 -r |
    awk '{print $1}'
)"
unset model_demo_token

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/.." && pwd)"
cleanup_service_account="${cleanup_service_account_id}@${PROOFSTITCH_PROJECT_ID}.iam.gserviceaccount.com"
project_number="$(gcloud projects describe "${PROOFSTITCH_PROJECT_ID}" --format='value(projectNumber)')"
if [[ ! "${project_number}" =~ ^[0-9]+$ ]]; then
  echo "Refusing deployment: the Google Cloud project number could not be verified." >&2
  exit 2
fi
cloud_tasks_service_agent="service-${project_number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
cloud_tasks_service_agent_identity="serviceAccount:${cloud_tasks_service_agent}"
cleanup_service_account_identity="serviceAccount:${cleanup_service_account}"
cleanup_service_account_resource="//iam.googleapis.com/projects/${PROOFSTITCH_PROJECT_ID}/serviceAccounts/${cleanup_service_account}"
service_resource="//run.googleapis.com/projects/${PROOFSTITCH_PROJECT_ID}/locations/${PROOFSTITCH_REGION}/services/${service_name}"
delete_api_url="https://run.googleapis.com/v2/projects/${PROOFSTITCH_PROJECT_ID}/locations/${PROOFSTITCH_REGION}/services/${service_name}"

existing_service="$(gcloud run services list \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --filter="metadata.name=${service_name}" \
  --format='value(metadata.name)')"
if [[ -n "${existing_service}" ]]; then
  echo "Refusing deployment: proofstitch already exists; use a fresh dedicated project or remove it explicitly." >&2
  exit 2
fi

existing_cleanup_account="$(gcloud iam service-accounts list \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --filter="email=${cleanup_service_account}" \
  --format='value(email)')"
if [[ -n "${existing_cleanup_account}" ]]; then
  echo "Refusing deployment: the dedicated cleanup service account already exists." >&2
  exit 2
fi

existing_cleanup_queue="$(gcloud tasks queues list \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --location="${PROOFSTITCH_TASKS_LOCATION}" \
  --filter="name:${cleanup_queue}" \
  --format='value(name)')"
if [[ -n "${existing_cleanup_queue}" ]]; then
  echo "Refusing deployment: the dedicated cleanup queue already exists." >&2
  exit 2
fi

cleanup_account_attempted=0
cleanup_queue_attempted=0
service_attempted=0
cleanup_task_attempted=0
cleanup_task_id=""
deployment_complete=0

wait_for_service_absence() {
  local attempt
  local matching_service
  for ((attempt = 1; attempt <= cleanup_poll_attempts; attempt++)); do
    if matching_service="$(gcloud run services list \
      --project="${PROOFSTITCH_PROJECT_ID}" \
      --region="${PROOFSTITCH_REGION}" \
      --filter="metadata.name=${service_name}" \
      --format='value(metadata.name)' 2>/dev/null)"; then
      if [[ -z "${matching_service}" ]]; then
        return 0
      fi
    fi
    if [[ "${attempt}" -lt "${cleanup_poll_attempts}" ]]; then
      sleep "${cleanup_poll_interval_seconds}"
    fi
  done
  return 1
}

cleanup_partial_deployment() {
  exit_status=$?
  if [[ "${deployment_complete}" -eq 1 ]]; then
    return
  fi

  set +e
  service_absent=1
  if [[ "${service_attempted}" -eq 1 ]]; then
    service_absent=0
    gcloud run services delete "${service_name}" \
      --project="${PROOFSTITCH_PROJECT_ID}" \
      --region="${PROOFSTITCH_REGION}" \
      --quiet >/dev/null 2>&1
    if wait_for_service_absence; then
      service_absent=1
    fi
  fi

  if [[ "${service_absent}" -eq 1 ]]; then
    if [[ "${cleanup_task_attempted}" -eq 1 && -n "${cleanup_task_id}" ]]; then
      if ! gcloud tasks delete "${cleanup_task_id}" \
        --project="${PROOFSTITCH_PROJECT_ID}" \
        --location="${PROOFSTITCH_TASKS_LOCATION}" \
        --queue="${cleanup_queue}" \
        --quiet >/dev/null 2>&1; then
        echo "Cleanup note: the scheduled task could not be removed after service deletion." >&2
      fi
    fi
    if [[ "${cleanup_queue_attempted}" -eq 1 ]]; then
      if ! gcloud tasks queues delete "${cleanup_queue}" \
        --project="${PROOFSTITCH_PROJECT_ID}" \
        --location="${PROOFSTITCH_TASKS_LOCATION}" \
        --quiet >/dev/null 2>&1; then
        echo "Cleanup note: the idle cleanup queue could not be removed." >&2
      fi
    fi
    if [[ "${cleanup_account_attempted}" -eq 1 ]]; then
      if ! gcloud iam service-accounts delete "${cleanup_service_account}" \
        --project="${PROOFSTITCH_PROJECT_ID}" \
        --quiet >/dev/null 2>&1; then
        echo "Cleanup note: the idle cleanup identity could not be removed." >&2
      fi
    fi
  else
    echo "Cleanup incomplete: Cloud Run absence was not confirmed; the scheduled task, queue, and cleanup identity were retained." >&2
    echo "Run scripts/cleanup_private.sh with the same project and region settings." >&2
  fi
  exit "${exit_status}"
}

trap cleanup_partial_deployment EXIT
trap 'exit 130' HUP INT TERM

cleanup_account_attempted=1
gcloud iam service-accounts create "${cleanup_service_account_id}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --display-name="ProofStitch one-shot cleanup" \
  --description="Deletes the temporary ProofStitch Cloud Run service" \
  --quiet >/dev/null

gcloud iam service-accounts add-iam-policy-binding "${cleanup_service_account}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --member="${cloud_tasks_service_agent_identity}" \
  --role=roles/iam.serviceAccountUser \
  --quiet >/dev/null

cleanup_queue_attempted=1
gcloud tasks queues create "${cleanup_queue}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --location="${PROOFSTITCH_TASKS_LOCATION}" \
  --max-attempts=5 \
  --max-retry-duration=300s \
  --min-backoff=10s \
  --max-backoff=60s \
  --max-doublings=2 \
  --max-concurrent-dispatches=1 \
  --max-dispatches-per-second=1 \
  --log-sampling-ratio=0 \
  --quiet >/dev/null

service_attempted=1
gcloud run deploy "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --source="${repo_dir}" \
  --service-account="${PROOFSTITCH_SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated \
  --invoker-iam-check \
  --ingress=all \
  --min=0 \
  --max=1 \
  --concurrency=1 \
  --timeout=60s \
  --cpu=1 \
  --memory=512Mi \
  --cpu-throttling \
  --no-cpu-boost \
  --execution-environment=gen2 \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROOFSTITCH_PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256=disabled,PROOFSTITCH_MODEL_DEMO_NOT_BEFORE=0,PROOFSTITCH_MODEL_DEMO_EXPIRES_AT=0" \
  --quiet >/dev/null

gcloud run services add-iam-policy-binding "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --member="serviceAccount:${cleanup_service_account}" \
  --role=roles/run.admin \
  --quiet >/dev/null

service_json="$(gcloud run services describe "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --format=json)"
if ! jq -e '
  [.. | objects | .["run.googleapis.com/invoker-iam-disabled"]?]
  | all(. != true and . != "true")
' >/dev/null <<<"${service_json}"; then
  echo "Refusing deployment result: the Cloud Run invoker IAM check is disabled." >&2
  exit 3
fi
service_url="$(jq -er '.status.url // .uri // empty' <<<"${service_json}")"
if [[ ! "${service_url}" =~ ^https://[A-Za-z0-9.-]+$ ]]; then
  echo "Refusing deployment result: Cloud Run returned an invalid service URL." >&2
  exit 3
fi

iam_policy_json="$(gcloud run services get-iam-policy "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --format=json)"
if ! jq -e '
  [.bindings[]?.members[]? | select(. == "allUsers" or . == "allAuthenticatedUsers")]
  | length == 0
' >/dev/null <<<"${iam_policy_json}"; then
  echo "Refusing deployment result: a public Cloud Run binding remains." >&2
  exit 3
fi
if ! jq -e --arg cleanup_member "serviceAccount:${cleanup_service_account}" '
  [.bindings[]? | select(.role == "roles/run.admin") | .members[]?]
  | index($cleanup_member) != null
' >/dev/null <<<"${iam_policy_json}"; then
  echo "Refusing deployment result: the one-shot cleanup identity lacks service-level delete authority." >&2
  exit 3
fi

ancestors_json="$(gcloud projects get-ancestors "${PROOFSTITCH_PROJECT_ID}" --format=json)"
if ! jq -e 'type == "array"' >/dev/null <<<"${ancestors_json}"; then
  echo "Refusing deployment result: the project hierarchy could not be verified." >&2
  exit 3
fi
organization_id="$(jq -r '[.[] | select(.type == "organization") | .id][0] // empty' <<<"${ancestors_json}")"
folder_count="$(jq '[.[] | select(.type == "folder")] | length' <<<"${ancestors_json}")"
if [[ -n "${organization_id}" ]]; then
  analyzer_scope="--organization=${organization_id}"
elif [[ "${folder_count}" -gt 0 ]]; then
  echo "Refusing deployment result: the full inherited IAM hierarchy could not be selected." >&2
  exit 3
else
  analyzer_scope="--project=${PROOFSTITCH_PROJECT_ID}"
fi

for anonymous_identity in allUsers allAuthenticatedUsers; do
  analysis_json="$(gcloud asset analyze-iam-policy \
    "${analyzer_scope}" \
    --full-resource-name="${service_resource}" \
    --identity="${anonymous_identity}" \
    --permissions=run.routes.invoke \
    --expand-resources \
    --expand-roles \
    --show-response \
    --format=json)"
  if ! jq -e '
    def keyed($name): [.. | objects | select(has($name)) | .[$name]];
    keyed("fullyExplored") as $explored
    | keyed("analysisResults") as $results
    | keyed("nonCriticalErrors") as $errors
    | ($explored | length) > 0
      and all($explored[]; . == true)
      and ($results | length) > 0
      and all($results[]; type == "array" and length == 0)
      and all($errors[]; type == "array" and length == 0)
  ' >/dev/null <<<"${analysis_json}"; then
    echo "Refusing deployment result: effective inherited IAM is public or incompletely analyzed." >&2
    exit 3
  fi
done

require_effective_permission() {
  local principal_email="$1"
  local resource="$2"
  local permission="$3"
  local resource_service="$4"
  local resource_type="$5"
  local description="$6"
  local attempt
  local request_time
  local troubleshoot_json

  for ((attempt = 1; attempt <= iam_poll_attempts; attempt++)); do
    request_time="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if troubleshoot_json="$(CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
      gcloud beta policy-intelligence troubleshoot-policy iam "${resource}" \
      --project="${PROOFSTITCH_PROJECT_ID}" \
      --principal-email="${principal_email}" \
      --permission="${permission}" \
      --resource-name="${resource}" \
      --resource-service="${resource_service}" \
      --resource-type="${resource_type}" \
      --request-time="${request_time}" \
      --format=json)" && jq -e '
        .overallAccessState == "CAN_ACCESS"
        and ((.errors // []) | type == "array" and length == 0)
      ' >/dev/null <<<"${troubleshoot_json}"; then
      return 0
    fi
    if [[ "${attempt}" -lt "${iam_poll_attempts}" ]]; then
      sleep "${iam_poll_interval_seconds}"
    fi
  done

  echo "Refusing deployment result: ${description} could not be verified." >&2
  return 1
}

if ! require_effective_permission \
  "${cleanup_service_account}" \
  "${service_resource}" \
  run.services.delete \
  run.googleapis.com \
  run.googleapis.com/Service \
  "effective cleanup delete authority"; then
  exit 3
fi
if ! require_effective_permission \
  "${cloud_tasks_service_agent}" \
  "${cleanup_service_account_resource}" \
  iam.serviceAccounts.actAs \
  iam.googleapis.com \
  iam.googleapis.com/ServiceAccount \
  "Cloud Tasks service-agent act-as authority"; then
  exit 3
fi
if ! require_effective_permission \
  "${cloud_tasks_service_agent}" \
  "${cleanup_service_account_resource}" \
  iam.serviceAccounts.getAccessToken \
  iam.googleapis.com \
  iam.googleapis.com/ServiceAccount \
  "Cloud Tasks service-agent access-token authority"; then
  exit 3
fi

anonymous_http_code="$(curl \
  --silent \
  --show-error \
  --output /dev/null \
  --write-out '%{http_code}' \
  --connect-timeout 10 \
  --max-time 20 \
  "${service_url}/healthz")"
if [[ "${anonymous_http_code}" != "401" && "${anonymous_http_code}" != "403" ]]; then
  echo "Refusing deployment result: an anonymous request reached the Cloud Run application." >&2
  exit 3
fi

deadline_epoch="$(( $(date +%s) + max_demo_window_seconds ))"
if deadline_rfc3339="$(date -u -r "${deadline_epoch}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"; then
  :
else
  deadline_rfc3339="$(date -u -d "@${deadline_epoch}" '+%Y-%m-%dT%H:%M:%SZ')"
fi
cleanup_task_id="delete-proofstitch-${deadline_epoch}"
cleanup_task_attempted=1
gcloud tasks create-http-task "${cleanup_task_id}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --location="${PROOFSTITCH_TASKS_LOCATION}" \
  --queue="${cleanup_queue}" \
  --url="${delete_api_url}" \
  --method=DELETE \
  --schedule-time="${deadline_rfc3339}" \
  --oauth-service-account-email="${cleanup_service_account}" \
  --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
  --quiet >/dev/null

task_json="$(gcloud tasks describe "${cleanup_task_id}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --location="${PROOFSTITCH_TASKS_LOCATION}" \
  --queue="${cleanup_queue}" \
  --format=json)"
if ! jq -e \
  --arg expected_url "${delete_api_url}" \
  --arg expected_account "${cleanup_service_account}" \
  --arg expected_schedule "${deadline_rfc3339}" \
  --arg expected_scope "https://www.googleapis.com/auth/cloud-platform" '
    .httpRequest.url == $expected_url
    and .httpRequest.httpMethod == "DELETE"
    and .httpRequest.oauthToken.serviceAccountEmail == $expected_account
    and .httpRequest.oauthToken.scope == $expected_scope
    and ((.scheduleTime | sub("\\.[0-9]+Z$"; "Z")) == $expected_schedule)
  ' >/dev/null <<<"${task_json}"; then
  echo "Refusing deployment result: the scheduled one-shot deletion task was not verified." >&2
  exit 3
fi

model_demo_not_before="$(date +%s)"
if [[ "${model_demo_not_before}" -ge "${deadline_epoch}" ]]; then
  echo "Refusing deployment result: no bounded demonstration window remains." >&2
  exit 3
fi
gcloud run services update "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --update-env-vars="PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256=${model_demo_token_sha256},PROOFSTITCH_MODEL_DEMO_NOT_BEFORE=${model_demo_not_before},PROOFSTITCH_MODEL_DEMO_EXPIRES_AT=${deadline_epoch}" \
  --quiet >/dev/null

anonymous_http_code="$(curl \
  --silent \
  --show-error \
  --output /dev/null \
  --write-out '%{http_code}' \
  --connect-timeout 10 \
  --max-time 20 \
  "${service_url}/healthz")"
if [[ "${anonymous_http_code}" != "401" && "${anonymous_http_code}" != "403" ]]; then
  echo "Refusing deployment result: the activated service is anonymously reachable." >&2
  exit 3
fi

deployment_complete=1
trap - EXIT HUP INT TERM
unset model_demo_token_sha256

echo "Verified private Cloud Run service with a one-shot deletion scheduled for ${deadline_rfc3339}."
echo "Run scripts/cleanup_private.sh immediately after recording; the scheduled task remains the fallback until service absence is confirmed."

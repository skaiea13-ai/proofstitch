#!/usr/bin/env bash
set -euo pipefail

service_name="proofstitch"
cleanup_queue="proofstitch-cleanup"
cleanup_service_account_id="proofstitch-cleanup"
cleanup_poll_attempts="${PROOFSTITCH_CLEANUP_POLL_ATTEMPTS:-30}"
cleanup_poll_interval_seconds="${PROOFSTITCH_CLEANUP_POLL_INTERVAL_SECONDS:-2}"

: "${PROOFSTITCH_PROJECT_ID:?Set the dedicated Google Cloud project ID.}"
: "${PROOFSTITCH_REGION:?Set the Cloud Run region.}"
: "${PROOFSTITCH_TASKS_LOCATION:?Set the Cloud Tasks location.}"

if [[ "${PROOFSTITCH_CLEANUP_CONFIRM:-}" != "DELETE_PRIVATE" ]]; then
  echo "Refusing cleanup: set PROOFSTITCH_CLEANUP_CONFIRM=DELETE_PRIVATE." >&2
  exit 2
fi
if [[ ! "${PROOFSTITCH_PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "Refusing cleanup: the Google Cloud project ID is invalid." >&2
  exit 2
fi
if [[ ! "${PROOFSTITCH_REGION}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] ||
  [[ ! "${PROOFSTITCH_TASKS_LOCATION}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Refusing cleanup: the Cloud Run region or Cloud Tasks location is invalid." >&2
  exit 2
fi
if [[ ! "${cleanup_poll_attempts}" =~ ^[1-9][0-9]*$ ]] ||
  [[ ! "${cleanup_poll_interval_seconds}" =~ ^[0-9]+$ ]]; then
  echo "Refusing cleanup: poll attempts and interval must be non-negative integers." >&2
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "Refusing cleanup: gcloud is required." >&2
  exit 2
fi

cleanup_service_account="${cleanup_service_account_id}@${PROOFSTITCH_PROJECT_ID}.iam.gserviceaccount.com"

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

if gcloud run services describe "${service_name}" \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --region="${PROOFSTITCH_REGION}" \
  --format='value(metadata.name)' >/dev/null 2>&1; then
  if ! gcloud run services delete "${service_name}" \
    --project="${PROOFSTITCH_PROJECT_ID}" \
    --region="${PROOFSTITCH_REGION}" \
    --quiet >/dev/null; then
    :
  fi
fi

if ! wait_for_service_absence; then
  echo "Cleanup incomplete: Cloud Run absence could not be confirmed; the scheduled fallback resources were retained." >&2
  exit 3
fi

if ! cleanup_queue_match="$(gcloud tasks queues list \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --location="${PROOFSTITCH_TASKS_LOCATION}" \
  --filter="name:${cleanup_queue}" \
  --format='value(name)' 2>/dev/null)"; then
  echo "Cleanup incomplete: the cleanup queue state could not be verified." >&2
  exit 3
fi
if [[ -n "${cleanup_queue_match}" ]] && ! gcloud tasks queues delete "${cleanup_queue}" \
    --project="${PROOFSTITCH_PROJECT_ID}" \
    --location="${PROOFSTITCH_TASKS_LOCATION}" \
    --quiet >/dev/null; then
  echo "Cleanup incomplete: the cleanup queue could not be removed." >&2
  exit 3
fi
if ! cleanup_account_match="$(gcloud iam service-accounts list \
  --project="${PROOFSTITCH_PROJECT_ID}" \
  --filter="email=${cleanup_service_account}" \
  --format='value(email)' 2>/dev/null)"; then
  echo "Cleanup incomplete: the cleanup identity state could not be verified." >&2
  exit 3
fi
if [[ -n "${cleanup_account_match}" ]] && ! gcloud iam service-accounts delete "${cleanup_service_account}" \
    --project="${PROOFSTITCH_PROJECT_ID}" \
    --quiet >/dev/null; then
  echo "Cleanup incomplete: the cleanup identity could not be removed." >&2
  exit 3
fi

echo "Confirmed that the ProofStitch Cloud Run service is absent; cleanup fallbacks were removed or already absent."

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_private.sh"
CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "cleanup_private.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${PROOFSTITCH_MODEL_DEMO_TOKEN:-}" ]]; then
  printf 'raw model demo token leaked to gcloud child environment\n' >&2
  exit 65
fi
if [[ -n "${PROOFSTITCH_SERVICE_ACCOUNT:-}" || -n "${runtime_service_account:-}" ]]; then
  printf 'raw runtime service account leaked to gcloud child environment\n' >&2
  exit 65
fi
for internal_name in model_demo_token gemini_secret_id gemini_secret_version; do
  if [[ -n "${!internal_name:-}" ]]; then
    printf 'internal deployment input leaked to gcloud child environment\n' >&2
    exit 65
  fi
done
if [[ -n "${GOOGLE_API_KEY:-}" || -n "${GEMINI_API_KEY:-}" ]]; then
  printf 'raw Gemini API key leaked to gcloud child environment\n' >&2
  exit 65
fi
printf '%s\n' "$*" >>"${FAKE_GCLOUD_LOG:?}"

case "${1:-} ${2:-} ${3:-}" in
  "projects describe proofstitch-security-test")
    printf '%s\n' '123456789012'
    ;;
  "secrets describe proofstitch-gemini-api-key")
    if [[ "${FAKE_SECRET_DESCRIBE_FAIL:-0}" == "1" ]]; then
      exit 72
    fi
    printf '%s\n' 'projects/123456789012/secrets/proofstitch-gemini-api-key'
    ;;
  "secrets versions describe")
    if [[ "${FAKE_SECRET_VERSION_DESCRIBE_FAIL:-0}" == "1" ]]; then
      exit 73
    fi
    printf '%s\n' "${FAKE_SECRET_VERSION_STATE:-ENABLED}"
    ;;
  "run services list")
    if [[ -f "${FAKE_SERVICE_DELETE_ATTEMPTED:?}" && "${FAKE_SERVICE_LIST_FAIL_AFTER_DELETE:-0}" == "1" ]]; then
      exit 69
    fi
    if [[ -n "${FAKE_EXISTING_SERVICE:-}" ]]; then
      printf '%s\n' "${FAKE_EXISTING_SERVICE}"
    elif [[ -f "${FAKE_SERVICE_DEPLOYED:?}" && ! -f "${FAKE_SERVICE_DELETED:?}" ]]; then
      printf '%s\n' 'proofstitch'
    fi
    ;;
  "iam service-accounts list")
    if [[ "${FAKE_ACCOUNT_LIST_FAIL:-0}" == "1" ]]; then
      exit 70
    fi
    printf '%s\n' "${FAKE_EXISTING_CLEANUP_ACCOUNT:-}"
    ;;
  "tasks queues list")
    if [[ "${FAKE_QUEUE_LIST_FAIL:-0}" == "1" ]]; then
      exit 71
    fi
    printf '%s\n' "${FAKE_EXISTING_CLEANUP_QUEUE:-}"
    ;;
  "iam service-accounts create"|"iam service-accounts delete"|"iam service-accounts add-iam-policy-binding")
    ;;
  "iam service-accounts describe")
    ;;
  "tasks queues create"|"tasks queues delete"|"tasks queues describe")
    ;;
  "run deploy proofstitch")
    : >"${FAKE_SERVICE_DEPLOYED:?}"
    ;;
  "run services add-iam-policy-binding")
    ;;
  "run services describe")
    if [[ -f "${FAKE_SERVICE_DELETED:?}" ]]; then
      exit 1
    fi
    printf '{"metadata":{"annotations":{"run.googleapis.com/invoker-iam-disabled":"%s"}},"status":{"url":"https://proofstitch.example.invalid"}}\n' \
      "${FAKE_INVOKER_DISABLED:-false}"
    ;;
  "run services get-iam-policy")
    if [[ -n "${FAKE_IAM_POLICY_JSON:-}" ]]; then
      printf '%s\n' "${FAKE_IAM_POLICY_JSON}"
    else
      printf '{"bindings":[{"role":"roles/run.admin","members":["serviceAccount:proofstitch-cleanup@%s.iam.gserviceaccount.com"]}]}\n' \
        "${PROOFSTITCH_PROJECT_ID}"
    fi
    ;;
  "projects get-ancestors proofstitch-security-test")
    if [[ -n "${FAKE_ANCESTORS_JSON:-}" ]]; then
      printf '%s\n' "${FAKE_ANCESTORS_JSON}"
    else
      printf '%s\n' '[{"type":"project","id":"proofstitch-security-test"}]'
    fi
    ;;
  "asset analyze-iam-policy --project=proofstitch-security-test"|"asset analyze-iam-policy --organization="*)
    if [[ "$*" == *"--identity=allUsers"* || "$*" == *"--identity=allAuthenticatedUsers"* ]]; then
      if [[ -n "${FAKE_ANALYSIS_JSON:-}" ]]; then
        printf '%s\n' "${FAKE_ANALYSIS_JSON}"
      else
        printf '%s\n' '{"mainAnalysis":{"analysisResults":[],"fullyExplored":true}}'
      fi
    else
      printf 'unexpected IAM analysis: %s\n' "$*" >&2
      exit 64
    fi
    ;;
  "beta policy-intelligence troubleshoot-policy")
    if [[ "$*" == *"--help"* ]]; then
      exit 0
    elif [[ "$*" == *"--permission=run.services.delete"* ]]; then
      if [[ -n "${FAKE_CLEANUP_DELETE_TROUBLESHOOT_JSON:-}" ]]; then
        printf '%s\n' "${FAKE_CLEANUP_DELETE_TROUBLESHOOT_JSON}"
      else
        printf '%s\n' '{"overallAccessState":"CAN_ACCESS","errors":[]}'
      fi
    elif [[ "$*" == *"--permission=iam.serviceAccounts.actAs"* || "$*" == *"--permission=iam.serviceAccounts.getAccessToken"* ]]; then
      if [[ -n "${FAKE_TOKEN_TROUBLESHOOT_JSON:-}" ]]; then
        printf '%s\n' "${FAKE_TOKEN_TROUBLESHOOT_JSON}"
      else
        printf '%s\n' '{"overallAccessState":"CAN_ACCESS","errors":[]}'
      fi
    else
      printf 'unexpected IAM troubleshoot: %s\n' "$*" >&2
      exit 64
    fi
    ;;
  "tasks create-http-task "*)
    if [[ "${FAKE_TASK_CREATE_FAIL:-0}" == "1" ]]; then
      exit 67
    fi
    task_url=""
    task_schedule=""
    task_account=""
    task_method=""
    task_scope=""
    for argument in "$@"; do
      case "${argument}" in
        --url=*) task_url="${argument#--url=}" ;;
        --schedule-time=*) task_schedule="${argument#--schedule-time=}" ;;
        --oauth-service-account-email=*) task_account="${argument#--oauth-service-account-email=}" ;;
        --oauth-token-scope=*) task_scope="${argument#--oauth-token-scope=}" ;;
        --method=*) task_method="${argument#--method=}" ;;
      esac
    done
    jq -n \
      --arg url "${task_url}" \
      --arg schedule "${task_schedule}" \
      --arg account "${task_account}" \
      --arg scope "${task_scope}" \
      --arg method "${task_method}" \
      '{httpRequest:{url:$url,httpMethod:$method,oauthToken:{serviceAccountEmail:$account,scope:$scope}},scheduleTime:$schedule}' \
      >"${FAKE_TASK_STATE:?}"
    ;;
  "tasks describe "*)
    if [[ "${FAKE_TASK_DESCRIBE_INVALID:-0}" == "1" ]]; then
      printf '{"httpRequest":{"url":"https://wrong.invalid","httpMethod":"POST","oauthToken":{"serviceAccountEmail":"wrong@example.invalid"}},"scheduleTime":"1970-01-01T00:00:00Z"}\n'
    else
      cat "${FAKE_TASK_STATE:?}"
    fi
    ;;
  "tasks delete "*)
    ;;
  "run services update")
    ;;
  "run services delete")
    : >"${FAKE_SERVICE_DELETE_ATTEMPTED:?}"
    if [[ "${FAKE_SERVICE_DELETE_FAIL:-0}" == "1" ]]; then
      exit 68
    fi
    : >"${FAKE_SERVICE_DELETED:?}"
    ;;
  *)
    printf 'unexpected gcloud invocation: %s\n' "$*" >&2
    exit 64
    ;;
esac
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${PROOFSTITCH_MODEL_DEMO_TOKEN:-}" ]]; then
  printf 'raw model demo token leaked to curl child environment\n' >&2
  exit 65
fi
if [[ -n "${PROOFSTITCH_SERVICE_ACCOUNT:-}" || -n "${runtime_service_account:-}" ]]; then
  printf 'raw runtime service account leaked to curl child environment\n' >&2
  exit 65
fi
for internal_name in model_demo_token gemini_secret_id gemini_secret_version; do
  if [[ -n "${!internal_name:-}" ]]; then
    printf 'internal deployment input leaked to curl child environment\n' >&2
    exit 65
  fi
done
printf '%s\n' "$*" >>"${FAKE_CURL_LOG:?}"
count=0
if [[ -f "${FAKE_CURL_COUNT:?}" ]]; then
  count="$(cat "${FAKE_CURL_COUNT}")"
fi
count=$((count + 1))
printf '%s' "${count}" >"${FAKE_CURL_COUNT}"
IFS=',' read -r -a codes <<<"${FAKE_ANON_HTTP_CODES:-403,403}"
index=$((count - 1))
printf '%s' "${codes[${index}]:-${codes[0]}}"
"""


def _run_deploy(
    tmp_path: Path,
    *,
    invoker_disabled: str = "false",
    iam_policy_json: str = "",
    model_demo_token: str = "a" * 64,
    existing_service: str = "",
    existing_cleanup_account: str = "",
    existing_cleanup_queue: str = "",
    analysis_json: str = "",
    anonymous_http_codes: str = "403,403",
    task_create_fail: bool = False,
    task_describe_invalid: bool = False,
    cleanup_delete_troubleshoot_json: str = "",
    token_troubleshoot_json: str = "",
    service_delete_fail: bool = False,
    service_list_fail_after_delete: bool = False,
    runtime_service_account: str = (
        "proofstitch-runtime@proofstitch-security-test.iam.gserviceaccount.com"
    ),
    gemini_secret: str = "proofstitch-gemini-api-key",
    gemini_secret_version: str = "1",
    gemini_free_tier_confirmed: str = "YES",
    raw_google_api_key: str | None = None,
    secret_describe_fail: bool = False,
    secret_version_describe_fail: bool = False,
    secret_version_state: str = "ENABLED",
    exported_internal_names: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(_FAKE_GCLOUD, encoding="utf-8")
    fake_gcloud.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(_FAKE_CURL, encoding="utf-8")
    fake_curl.chmod(0o755)
    gcloud_log = tmp_path / "gcloud.log"
    curl_log = tmp_path / "curl.log"
    env: dict[str, str] = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROOFSTITCH_PROJECT_ID": "proofstitch-security-test",
        "PROOFSTITCH_REGION": "us-central1",
        "PROOFSTITCH_TASKS_LOCATION": "us-central1",
        "PROOFSTITCH_SERVICE_ACCOUNT": runtime_service_account,
        "PROOFSTITCH_DEPLOY_CONFIRM": "DEPLOY_PRIVATE",
        "PROOFSTITCH_NO_COST_CONFIRMED": "YES",
        "PROOFSTITCH_GEMINI_FREE_TIER_CONFIRMED": gemini_free_tier_confirmed,
        "PROOFSTITCH_GEMINI_SECRET": gemini_secret,
        "PROOFSTITCH_GEMINI_SECRET_VERSION": gemini_secret_version,
        "PROOFSTITCH_MODEL_DEMO_TOKEN": model_demo_token,
        "FAKE_GCLOUD_LOG": str(gcloud_log),
        "FAKE_CURL_LOG": str(curl_log),
        "FAKE_CURL_COUNT": str(tmp_path / "curl.count"),
        "FAKE_TASK_STATE": str(tmp_path / "task.json"),
        "FAKE_SERVICE_DEPLOYED": str(tmp_path / "service.deployed"),
        "FAKE_SERVICE_DELETE_ATTEMPTED": str(tmp_path / "service.delete-attempted"),
        "FAKE_SERVICE_DELETED": str(tmp_path / "service.deleted"),
        "FAKE_INVOKER_DISABLED": invoker_disabled,
        "FAKE_IAM_POLICY_JSON": iam_policy_json,
        "FAKE_EXISTING_SERVICE": existing_service,
        "FAKE_EXISTING_CLEANUP_ACCOUNT": existing_cleanup_account,
        "FAKE_EXISTING_CLEANUP_QUEUE": existing_cleanup_queue,
        "FAKE_ANALYSIS_JSON": analysis_json,
        "FAKE_CLEANUP_DELETE_TROUBLESHOOT_JSON": cleanup_delete_troubleshoot_json,
        "FAKE_TOKEN_TROUBLESHOOT_JSON": token_troubleshoot_json,
        "FAKE_ANON_HTTP_CODES": anonymous_http_codes,
        "FAKE_TASK_CREATE_FAIL": "1" if task_create_fail else "0",
        "FAKE_TASK_DESCRIBE_INVALID": "1" if task_describe_invalid else "0",
        "FAKE_SERVICE_DELETE_FAIL": "1" if service_delete_fail else "0",
        "FAKE_SERVICE_LIST_FAIL_AFTER_DELETE": (
            "1" if service_list_fail_after_delete else "0"
        ),
        "FAKE_SECRET_DESCRIBE_FAIL": "1" if secret_describe_fail else "0",
        "FAKE_SECRET_VERSION_DESCRIBE_FAIL": (
            "1" if secret_version_describe_fail else "0"
        ),
        "FAKE_SECRET_VERSION_STATE": secret_version_state,
        "PROOFSTITCH_CLEANUP_POLL_ATTEMPTS": "2",
        "PROOFSTITCH_CLEANUP_POLL_INTERVAL_SECONDS": "0",
        "PROOFSTITCH_IAM_POLL_ATTEMPTS": "2",
        "PROOFSTITCH_IAM_POLL_INTERVAL_SECONDS": "0",
    }
    env.pop("GOOGLE_API_KEY", None)
    env.pop("GEMINI_API_KEY", None)
    if raw_google_api_key is not None:
        env["GOOGLE_API_KEY"] = raw_google_api_key
    if exported_internal_names:
        for internal_name in (
            "model_demo_token",
            "runtime_service_account",
            "gemini_secret_id",
            "gemini_secret_version",
        ):
            env[internal_name] = "ambient-sentinel"
    result = subprocess.run(
        [str(DEPLOY_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    gcloud_calls = gcloud_log.read_text(encoding="utf-8") if gcloud_log.exists() else ""
    curl_calls = curl_log.read_text(encoding="utf-8") if curl_log.exists() else ""
    return result, gcloud_calls, curl_calls


def _run_cleanup(
    tmp_path: Path,
    *,
    service_delete_fail: bool = False,
    service_list_fail_after_delete: bool = False,
    queue_list_fail: bool = False,
    account_list_fail: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(_FAKE_GCLOUD, encoding="utf-8")
    fake_gcloud.chmod(0o755)
    gcloud_log = tmp_path / "gcloud.log"
    service_deployed = tmp_path / "service.deployed"
    service_deployed.touch()
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PROOFSTITCH_PROJECT_ID": "proofstitch-security-test",
        "PROOFSTITCH_REGION": "us-central1",
        "PROOFSTITCH_TASKS_LOCATION": "us-central1",
        "PROOFSTITCH_CLEANUP_CONFIRM": "DELETE_PRIVATE",
        "PROOFSTITCH_CLEANUP_POLL_ATTEMPTS": "2",
        "PROOFSTITCH_CLEANUP_POLL_INTERVAL_SECONDS": "0",
        "FAKE_GCLOUD_LOG": str(gcloud_log),
        "FAKE_SERVICE_DEPLOYED": str(service_deployed),
        "FAKE_SERVICE_DELETE_ATTEMPTED": str(tmp_path / "service.delete-attempted"),
        "FAKE_SERVICE_DELETED": str(tmp_path / "service.deleted"),
        "FAKE_SERVICE_DELETE_FAIL": "1" if service_delete_fail else "0",
        "FAKE_SERVICE_LIST_FAIL_AFTER_DELETE": (
            "1" if service_list_fail_after_delete else "0"
        ),
        "FAKE_EXISTING_CLEANUP_ACCOUNT": (
            "proofstitch-cleanup@proofstitch-security-test.iam.gserviceaccount.com"
        ),
        "FAKE_EXISTING_CLEANUP_QUEUE": "proofstitch-cleanup",
        "FAKE_QUEUE_LIST_FAIL": "1" if queue_list_fail else "0",
        "FAKE_ACCOUNT_LIST_FAIL": "1" if account_list_fail else "0",
    }
    result = subprocess.run(
        [str(CLEANUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    gcloud_calls = gcloud_log.read_text(encoding="utf-8") if gcloud_log.exists() else ""
    return result, gcloud_calls


def test_private_deploy_establishes_bounded_private_lifecycle(tmp_path: Path) -> None:
    result, gcloud_calls, curl_calls = _run_deploy(
        tmp_path,
        exported_internal_names=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--no-allow-unauthenticated" in gcloud_calls
    assert "--invoker-iam-check" in gcloud_calls
    assert "a" * 64 not in gcloud_calls
    assert hashlib.sha256(("a" * 64).encode("ascii")).hexdigest() in gcloud_calls
    assert "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256=disabled" in gcloud_calls
    assert "secrets describe proofstitch-gemini-api-key" in gcloud_calls
    assert "secrets versions describe 1" in gcloud_calls
    assert "GOOGLE_GENAI_USE_ENTERPRISE=false" in gcloud_calls
    assert "GOOGLE_GENAI_USE_VERTEXAI=true" not in gcloud_calls
    assert "GOOGLE_CLOUD_PROJECT=" not in gcloud_calls
    assert "GOOGLE_CLOUD_LOCATION=" not in gcloud_calls
    assert (
        "--set-secrets=GOOGLE_API_KEY=proofstitch-gemini-api-key:1"
        in gcloud_calls
    )
    assert "roles/run.admin" in gcloud_calls
    assert "roles/iam.serviceAccountUser" in gcloud_calls
    assert "service-123456789012@gcp-sa-cloudtasks.iam.gserviceaccount.com" in gcloud_calls
    assert "tasks create-http-task delete-proofstitch-" in gcloud_calls
    assert "--method=DELETE" in gcloud_calls
    assert "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform" in gcloud_calls
    assert gcloud_calls.count("asset analyze-iam-policy") == 2
    assert gcloud_calls.count("beta policy-intelligence troubleshoot-policy") == 4
    assert "--identity=allUsers" in gcloud_calls
    assert "--identity=allAuthenticatedUsers" in gcloud_calls
    assert "--permission=run.services.delete" in gcloud_calls
    assert "--permission=iam.serviceAccounts.actAs" in gcloud_calls
    assert "--permission=iam.serviceAccounts.getAccessToken" in gcloud_calls
    assert gcloud_calls.index("tasks create-http-task") < gcloud_calls.index(
        "run services update"
    )
    not_before = int(
        re.findall(r"PROOFSTITCH_MODEL_DEMO_NOT_BEFORE=(\d+)", gcloud_calls)[-1]
    )
    expires_at = int(
        re.findall(r"PROOFSTITCH_MODEL_DEMO_EXPIRES_AT=(\d+)", gcloud_calls)[-1]
    )
    assert 0 < expires_at - not_before <= 10 * 60
    assert curl_calls.count("https://proofstitch.example.invalid/healthz") == 2
    assert "one-shot deletion scheduled" in result.stdout
    assert "cleanup_private.sh immediately after recording" in result.stdout


def test_private_deploy_rejects_disabled_invoker_iam_check(tmp_path: Path) -> None:
    result, gcloud_calls, _ = _run_deploy(tmp_path, invoker_disabled="true")

    assert result.returncode == 3
    assert "invoker IAM check is disabled" in result.stderr
    assert "run services update" not in gcloud_calls
    assert "run services delete proofstitch" in gcloud_calls


def test_private_deploy_rejects_public_service_binding(tmp_path: Path) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        iam_policy_json=(
            '{"bindings":[{"role":"roles/run.invoker",'
            '"members":["allUsers"]}]}'
        ),
    )

    assert result.returncode == 3
    assert "public Cloud Run binding remains" in result.stderr
    assert "run services update" not in gcloud_calls


def test_private_deploy_rejects_inherited_public_access(tmp_path: Path) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        analysis_json=(
            '{"mainAnalysis":{"analysisResults":[{"attachedResourceFullName":'
            '"//cloudresourcemanager.googleapis.com/projects/example"}],'
            '"fullyExplored":true}}'
        ),
    )

    assert result.returncode == 3
    assert "effective inherited IAM is public" in result.stderr
    assert "tasks create-http-task" not in gcloud_calls
    assert "run services update" not in gcloud_calls
    assert "run services delete proofstitch" in gcloud_calls


def test_private_deploy_rejects_incomplete_iam_analysis(tmp_path: Path) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        analysis_json=(
            '{"mainAnalysis":{"analysisResults":[],"fullyExplored":false,'
            '"nonCriticalErrors":[{"code":"PARTIAL"}]}}'
        ),
    )

    assert result.returncode == 3
    assert "incompletely analyzed" in result.stderr
    assert "run services update" not in gcloud_calls


@pytest.mark.parametrize("anonymous_code", ["200", "503"])
def test_private_deploy_rejects_anonymous_application_response(
    tmp_path: Path,
    anonymous_code: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        anonymous_http_codes=f"{anonymous_code},{anonymous_code}",
    )

    assert result.returncode == 3
    assert "anonymous request reached" in result.stderr
    assert "tasks create-http-task" not in gcloud_calls
    assert "run services update" not in gcloud_calls


def test_private_deploy_cleans_up_if_post_activation_probe_is_public(
    tmp_path: Path,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        anonymous_http_codes="403,200",
    )

    assert result.returncode == 3
    assert "activated service is anonymously reachable" in result.stderr
    assert "run services update" in gcloud_calls
    assert "run services delete proofstitch" in gcloud_calls
    assert "tasks delete delete-proofstitch-" in gcloud_calls
    assert "tasks queues delete proofstitch-cleanup" in gcloud_calls
    assert "iam service-accounts delete" in gcloud_calls
    assert gcloud_calls.index("run services delete proofstitch") < gcloud_calls.index(
        "tasks delete delete-proofstitch-"
    )


def test_private_deploy_retains_fallback_when_service_absence_is_unconfirmed(
    tmp_path: Path,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        anonymous_http_codes="403,200",
        service_delete_fail=True,
    )

    assert result.returncode == 3
    assert "Cloud Run absence was not confirmed" in result.stderr
    assert "run services delete proofstitch" in gcloud_calls
    assert "tasks delete delete-proofstitch-" not in gcloud_calls
    assert "tasks queues delete proofstitch-cleanup" not in gcloud_calls
    assert "iam service-accounts delete" not in gcloud_calls


def test_private_deploy_retains_fallback_when_service_state_is_unknown(
    tmp_path: Path,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        anonymous_http_codes="403,200",
        service_delete_fail=True,
        service_list_fail_after_delete=True,
    )

    assert result.returncode == 3
    assert "Cloud Run absence was not confirmed" in result.stderr
    assert "tasks delete delete-proofstitch-" not in gcloud_calls
    assert "tasks queues delete proofstitch-cleanup" not in gcloud_calls
    assert "iam service-accounts delete" not in gcloud_calls


@pytest.mark.parametrize(
    (
        "cleanup_delete_troubleshoot_json",
        "token_troubleshoot_json",
        "expected_message",
    ),
    [
        (
            '{"overallAccessState":"CANNOT_ACCESS","errors":[]}',
            "",
            "effective cleanup delete authority",
        ),
        (
            "",
            '{"overallAccessState":"UNKNOWN_CONDITIONAL","errors":[]}',
            "Cloud Tasks service-agent act-as authority",
        ),
    ],
)
def test_private_deploy_rejects_unverified_cleanup_authority(
    tmp_path: Path,
    cleanup_delete_troubleshoot_json: str,
    token_troubleshoot_json: str,
    expected_message: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        cleanup_delete_troubleshoot_json=cleanup_delete_troubleshoot_json,
        token_troubleshoot_json=token_troubleshoot_json,
    )

    assert result.returncode == 3
    assert expected_message in result.stderr
    assert "tasks create-http-task" not in gcloud_calls
    assert "run services update" not in gcloud_calls
    assert "run services delete proofstitch" in gcloud_calls


@pytest.mark.parametrize(
    ("task_create_fail", "task_describe_invalid"),
    [(True, False), (False, True)],
)
def test_private_deploy_never_activates_without_verified_cleanup_task(
    tmp_path: Path,
    task_create_fail: bool,
    task_describe_invalid: bool,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        task_create_fail=task_create_fail,
        task_describe_invalid=task_describe_invalid,
    )

    assert result.returncode != 0
    assert "run services update" not in gcloud_calls
    assert "run services delete proofstitch" in gcloud_calls


def test_private_deploy_rejects_invalid_credential_inputs_before_child_commands(
    tmp_path: Path,
) -> None:
    cases = [
        (
            "predictable",
            "proofstitch-runtime@proofstitch-security-test.iam.gserviceaccount.com",
            "64 lowercase hexadecimal characters",
        ),
        ("a" * 64, "AIza" + "A" * 35, "runtime service account is invalid"),
        ("a" * 64, "b" * 64, "runtime service account is invalid"),
        (
            "a" * 64,
            "proofstitch-runtime@another-project.iam.gserviceaccount.com",
            "runtime service account is invalid",
        ),
        (
            "a" * 64,
            "proofstitch-cleanup@proofstitch-security-test.iam.gserviceaccount.com",
            "reserved cleanup identity",
        ),
    ]

    for index, (model_demo_token, runtime_service_account, expected_message) in enumerate(
        cases
    ):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        result, gcloud_calls, _ = _run_deploy(
            case_path,
            model_demo_token=model_demo_token,
            runtime_service_account=runtime_service_account,
        )

        assert result.returncode == 2
        assert expected_message in result.stderr
        assert gcloud_calls == ""


@pytest.mark.parametrize(
    ("gemini_secret", "gemini_secret_version", "expected_message"),
    [
        ("bad/secret", "1", "Secret Manager secret ID is invalid"),
        ("AIza" + "A" * 35, "1", "Secret Manager secret ID is invalid"),
        ("proofstitch-gemini-api-key", "latest", "pinned positive integer"),
    ],
)
def test_private_deploy_rejects_unpinned_or_invalid_secret_reference(
    tmp_path: Path,
    gemini_secret: str,
    gemini_secret_version: str,
    expected_message: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        gemini_secret=gemini_secret,
        gemini_secret_version=gemini_secret_version,
    )

    assert result.returncode == 2
    assert expected_message in result.stderr
    assert gcloud_calls == ""


@pytest.mark.parametrize(
    ("secret_describe_fail", "secret_version_describe_fail", "secret_version_state"),
    [
        (True, False, "ENABLED"),
        (False, True, "ENABLED"),
        (False, False, "DISABLED"),
    ],
)
def test_private_deploy_rejects_missing_or_disabled_secret_before_mutation(
    tmp_path: Path,
    secret_describe_fail: bool,
    secret_version_describe_fail: bool,
    secret_version_state: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        secret_describe_fail=secret_describe_fail,
        secret_version_describe_fail=secret_version_describe_fail,
        secret_version_state=secret_version_state,
    )

    assert result.returncode == 2
    assert "Gemini API key secret" in result.stderr
    assert "service-accounts create" not in gcloud_calls
    assert "tasks queues create" not in gcloud_calls
    assert "run deploy" not in gcloud_calls


@pytest.mark.parametrize(
    ("gemini_free_tier_confirmed", "raw_google_api_key", "expected_message"),
    [
        ("NO", None, "Gemini Developer API free tier is not confirmed"),
        ("YES", "deliberately-set-test-value", "raw Gemini API key"),
    ],
)
def test_private_deploy_rejects_unconfirmed_free_tier_or_raw_api_key(
    tmp_path: Path,
    gemini_free_tier_confirmed: str,
    raw_google_api_key: str | None,
    expected_message: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        gemini_free_tier_confirmed=gemini_free_tier_confirmed,
        raw_google_api_key=raw_google_api_key,
    )

    assert result.returncode == 2
    assert expected_message in result.stderr
    assert gcloud_calls == ""


def test_private_deploy_rejects_existing_service_without_mutation(tmp_path: Path) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        existing_service="proofstitch",
    )

    assert result.returncode == 2
    assert "proofstitch already exists" in result.stderr
    assert " service-accounts create " not in f" {gcloud_calls} "
    assert "tasks queues create" not in gcloud_calls
    assert "run deploy" not in gcloud_calls


@pytest.mark.parametrize(
    ("existing_cleanup_account", "existing_cleanup_queue", "expected_message"),
    [
        ("proofstitch-cleanup@example.invalid", "", "service account already exists"),
        ("", "proofstitch-cleanup", "cleanup queue already exists"),
    ],
)
def test_private_deploy_rejects_reused_cleanup_resources(
    tmp_path: Path,
    existing_cleanup_account: str,
    existing_cleanup_queue: str,
    expected_message: str,
) -> None:
    result, gcloud_calls, _ = _run_deploy(
        tmp_path,
        existing_cleanup_account=existing_cleanup_account,
        existing_cleanup_queue=existing_cleanup_queue,
    )

    assert result.returncode == 2
    assert expected_message in result.stderr
    assert "run deploy" not in gcloud_calls


def test_cleanup_script_removes_fallbacks_only_after_service_absence(
    tmp_path: Path,
) -> None:
    result, gcloud_calls = _run_cleanup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Cloud Run service is absent" in result.stdout
    assert "run services delete proofstitch" in gcloud_calls
    assert "tasks queues delete proofstitch-cleanup" in gcloud_calls
    assert "iam service-accounts delete" in gcloud_calls
    assert gcloud_calls.index("run services delete proofstitch") < gcloud_calls.index(
        "tasks queues delete proofstitch-cleanup"
    )


def test_cleanup_script_retains_fallbacks_if_service_still_exists(
    tmp_path: Path,
) -> None:
    result, gcloud_calls = _run_cleanup(tmp_path, service_delete_fail=True)

    assert result.returncode == 3
    assert "scheduled fallback resources were retained" in result.stderr
    assert "tasks queues delete proofstitch-cleanup" not in gcloud_calls
    assert "iam service-accounts delete" not in gcloud_calls


def test_cleanup_script_retains_fallbacks_if_service_state_is_unknown(
    tmp_path: Path,
) -> None:
    result, gcloud_calls = _run_cleanup(
        tmp_path,
        service_delete_fail=True,
        service_list_fail_after_delete=True,
    )

    assert result.returncode == 3
    assert "scheduled fallback resources were retained" in result.stderr
    assert "tasks queues delete proofstitch-cleanup" not in gcloud_calls
    assert "iam service-accounts delete" not in gcloud_calls
    assert "Confirmed that" not in result.stdout


@pytest.mark.parametrize(
    ("queue_list_fail", "account_list_fail", "expected_message"),
    [
        (True, False, "cleanup queue state could not be verified"),
        (False, True, "cleanup identity state could not be verified"),
    ],
)
def test_cleanup_script_never_claims_success_for_unknown_fallback_state(
    tmp_path: Path,
    queue_list_fail: bool,
    account_list_fail: bool,
    expected_message: str,
) -> None:
    result, _ = _run_cleanup(
        tmp_path,
        queue_list_fail=queue_list_fail,
        account_list_fail=account_list_fail,
    )

    assert result.returncode == 3
    assert expected_message in result.stderr
    assert "Confirmed that" not in result.stdout

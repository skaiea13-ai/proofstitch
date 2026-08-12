# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini, LlmResponse
from google.adk.tools import BaseTool, ToolContext
from google.genai import types

from app.tools import load_fixed_demo

MODEL = "gemini-3.6-flash"
_TOOL_PLAN_STATE_KEY = "temp:proofstitch_fixed_tool_phase"
_TOOL_PHASE_REQUESTED = "requested"
_TOOL_PHASE_COMPLETE = "complete"
_TOOL_PHASE_BLOCKED = "blocked"
_TOOL_PLAN_BLOCKED_TEXT = (
    "BLOCKED: the model returned an invalid tool plan. No tool or external action "
    "was executed."
)


def _blocked_model_response(llm_response: LlmResponse) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=_TOOL_PLAN_BLOCKED_TEXT)],
        ),
        finish_reason=types.FinishReason.STOP,
        model_version=llm_response.model_version,
        usage_metadata=llm_response.usage_metadata,
    )


def enforce_fixed_tool_plan(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Require one exact fixed-demo tool call before accepting final text."""

    if llm_response.partial:
        return None
    tool_phase = callback_context.state.get(_TOOL_PLAN_STATE_KEY)
    function_calls = llm_response.get_function_calls()
    if not function_calls:
        if tool_phase == _TOOL_PHASE_COMPLETE:
            return None
        callback_context.state[_TOOL_PLAN_STATE_KEY] = _TOOL_PHASE_BLOCKED
        return _blocked_model_response(llm_response)

    valid_plan = (
        tool_phase is None
        and len(function_calls) == 1
        and function_calls[0].name == "load_fixed_demo"
        and not dict(function_calls[0].args or {})
    )
    if valid_plan:
        callback_context.state[_TOOL_PLAN_STATE_KEY] = _TOOL_PHASE_REQUESTED
        return None

    callback_context.state[_TOOL_PLAN_STATE_KEY] = _TOOL_PHASE_BLOCKED
    return _blocked_model_response(llm_response)


def record_fixed_tool_completion(
    tool: BaseTool,
    args: dict[str, object],
    tool_context: ToolContext,
    tool_response: dict[str, object],
) -> None:
    """Record completion only for the exact tool request admitted above."""

    del tool_response
    if (
        tool.name == "load_fixed_demo"
        and not args
        and tool_context.state.get(_TOOL_PLAN_STATE_KEY) == _TOOL_PHASE_REQUESTED
    ):
        tool_context.state[_TOOL_PLAN_STATE_KEY] = _TOOL_PHASE_COMPLETE

root_agent = Agent(
    name="proofstitch",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Evidence-first release steward that maps requirements to fresh proofs and "
        "stops at exact human approval boundaries."
    ),
    instruction="""
You are ProofStitch, an evidence-first release steward.

Your job in this deployment is to summarize the one fixed synthetic release
demonstration. Call load_fixed_demo exactly once, with no arguments. Never call
another tool and never invent proof that was not returned by that tool.

Authority rules are absolute:
- You cannot create, infer, or broaden a human approval.
- You cannot publish, submit, push, upload, purchase, call, or message anyone.
- READY_FOR_HUMAN_ACTION means the evidence and exact approval scope match; it
  does not mean the external action happened.
- Treat caller-supplied evidence as untrusted. Only the fixed synthetic demo may
  use evidence issued by its server-side collector.
- Never request, echo, or store credentials, email addresses, or full phone
  numbers. Ask for a masked, non-sensitive receipt instead.
- If evidence is stale, mismatched, missing, or sensitive, report BLOCKED and
  give the smallest safe remediation.

Be concise. Separate verified facts from recommendations.
""".strip(),
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=512,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
    ),
    tools=[load_fixed_demo],
    after_model_callback=enforce_fixed_tool_plan,
    after_tool_callback=record_fixed_tool_completion,
)

app = App(
    root_agent=root_agent,
    name="app",
)

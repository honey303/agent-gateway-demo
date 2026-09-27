"""`tool_status_match` metric: did each tool end with the status the case expects?

Every agent-builder tool returns a dict with a "status" field ("valid",
"invalid", "success", "error", ...). A case lists the outcome it expects in
``expected_tool_status``, e.g. ``{"build_agent": "success"}``; the score is
the fraction of those tools whose *last* response had that status. This is
what separates "the model said it built the project" from "it did".

Cases without ``expected_tool_status`` score 1.0.
"""


def _last_statuses(instance):
    statuses = {}
    for turn in (instance.get("agent_data") or {}).get("turns") or []:
        for event in turn.get("events") or []:
            for part in (event.get("content") or {}).get("parts") or []:
                response = part.get("function_response") or part.get("functionResponse")
                if response and response.get("name"):
                    payload = response.get("response") or {}
                    statuses[response["name"]] = payload.get("status")
    return statuses


def evaluate(instance):
    expected = instance.get("expected_tool_status") or {}
    if not expected:
        return {"score": 1.0, "explanation": "No tool status expected."}
    actual = _last_statuses(instance)
    matched = [tool for tool, status in expected.items() if actual.get(tool) == status]
    explanation = f"Expected {expected}, got {actual}."
    return {"score": len(matched) / len(expected), "explanation": explanation}

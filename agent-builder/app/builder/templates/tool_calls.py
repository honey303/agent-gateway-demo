"""Deterministic `tool_call_match` metric for `agents-cli eval grade`.

Scores the fraction of a case's ``expected_tool_calls`` that the agent
actually called anywhere in its trace (order-insensitive; extra calls are
fine). Cases that expect no tools score 1.0. Calling anything listed in the
optional ``forbidden_tool_calls`` scores 0. No model is involved, so it
catches the classic false pass: a fluent answer the model made up instead of
calling the tool.

agents-cli compiles this file's source and calls ``evaluate(instance)`` once
per case. Keep it self-contained: no imports from the project.
"""


def _called_tools(instance):
    names = []
    for turn in (instance.get("agent_data") or {}).get("turns") or []:
        for event in turn.get("events") or []:
            for part in (event.get("content") or {}).get("parts") or []:
                call = part.get("function_call") or part.get("functionCall")
                if call and call.get("name"):
                    names.append(call["name"])
    return names


def evaluate(instance):
    expected = instance.get("expected_tool_calls") or []
    forbidden = instance.get("forbidden_tool_calls") or []
    called = _called_tools(instance)
    violations = sorted({name for name in called if name in forbidden})
    if violations:
        return {"score": 0.0, "explanation": f"Called forbidden tools {violations}."}
    if not expected:
        return {"score": 1.0, "explanation": f"No tools expected. Called: {called}"}
    missing = [name for name in expected if name not in called]
    score = (len(expected) - len(missing)) / len(expected)
    explanation = f"Expected {expected}, called {called}."
    if missing:
        explanation += f" Missing: {missing}."
    return {"score": score, "explanation": explanation}

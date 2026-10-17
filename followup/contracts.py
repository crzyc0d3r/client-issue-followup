"""Validate input exports and the service responses before making decisions."""

import math
import re


NO_MATCH = "no_match"


def probability(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be finite and between zero and one")
    return float(value)


def validate_inputs(requests, issues):
    for rows, fields in ((requests, {"id", "client", "email", "text"}),
                         (issues, {"id", "title", "body", "state", "url"})):
        if not isinstance(rows, list) or not rows:
            raise ValueError("Request and issue exports must be nonempty arrays")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not fields <= set(row):
                raise ValueError(f"Required fields: {sorted(fields)}")
            if any(not isinstance(row[field], str) or not row[field].strip() for field in fields):
                raise ValueError("Required values must be nonempty strings")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["id"]):
                raise ValueError("IDs must be short letters, digits, underscores or hyphens")
            if row["id"] in seen:
                raise ValueError(f"Duplicate ID: {row['id']}")
            seen.add(row["id"])
    if len(issues) > 254:
        raise ValueError("At most 254 issue candidates plus no_match fit a choice question")
    for request in requests:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", request["email"]):
            raise ValueError("Invalid client email")
        if "\n" in request["email"] or "\r" in request["email"]:
            raise ValueError("Email must be one line")
    for issue in issues:
        if issue["state"] not in {"open", "closed"}:
            raise ValueError("Issue state must be open or closed")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*", issue["url"]):
            raise ValueError("Issue URLs must be HTTPS GitHub issue links")


def choices_for(issues):
    choices = {f"issue_{issue['id']}": f"{issue['title']}\n{issue['body']}" for issue in issues}
    choices[NO_MATCH] = "None of the listed issues addresses the client's request."
    return choices


def validate_choice_response(response, choices):
    if not isinstance(response, dict) or not isinstance(response.get("model"), str):
        raise ValueError("Decision response must name its model")
    if not isinstance(response.get("answers"), dict) or set(response["answers"]) != {"match"}:
        raise ValueError("Decision response must answer exactly the match question")
    answer = response["answers"]["match"]
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("Expected a choice answer")
    if answer.get("choice") not in choices:
        raise ValueError("Decision selected an unknown issue")
    probability(answer.get("confidence"), "confidence")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(choices):
        raise ValueError("Probabilities must cover exactly the submitted choices")
    for key, value in probabilities.items():
        probability(value, key)
    if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01):
        raise ValueError("Choice probabilities must sum approximately to one")
    if probabilities[answer["choice"]] < max(probabilities.values()):
        raise ValueError("Selected choice must have the highest probability")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Decision response must include token usage")
    for field in ("input_tokens", "output_tokens"):
        if type(usage.get(field)) is not int or usage[field] < 0:
            raise ValueError("Token usage must be nonnegative integers")
    return dict(answer)


def validate_review(response, choices):
    if not isinstance(response, dict) or set(response) != {"choice", "reason"}:
        raise ValueError("Reviewer must return exactly choice and reason")
    if response["choice"] not in choices:
        raise ValueError("Reviewer selected an unknown issue")
    if not isinstance(response["reason"], str) or not response["reason"].strip():
        raise ValueError("Reviewer must explain its choice")
    return response

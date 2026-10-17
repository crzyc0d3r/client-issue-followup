"""Match requests, escalate uncertainty, and checkpoint proposed writes locally."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .contracts import NO_MATCH, probability, validate_inputs


def run(requests, issues, decider, reviewer, threshold=0.8, mode="live"):
    validate_inputs(requests, issues)
    probability(threshold, "threshold")
    by_choice = {f"issue_{issue['id']}": issue for issue in issues}
    matches, proposals = [], []
    for request in requests:
        decision = decider.decide(request, issues)
        choice = decision["choice"]
        review = None
        if decision["confidence"] < threshold:
            review = reviewer.review(request, issues, decision)
            choice = review["choice"]
        issue = None if choice == NO_MATCH else by_choice[choice]
        matches.append({"request_id": request["id"], "initial_choice": decision["choice"],
                        "choice": choice, "confidence": decision["confidence"],
                        "reviewed": review is not None, "review": review})
        if issue:
            status = "marked closed" if issue["state"] == "closed" else "still open"
            message = (f"Hello {request['client']},\n\nYour request: {request['text']}\n\n"
                       f"The matching tracked issue is {issue['title']}: {issue['url']}. "
                       f"It is currently {status}. Please confirm whether this addresses your request.\n")
            actions = [("github.issue_comment", {"issue_url": issue["url"],
                        "body": f"Client follow-up draft for request {request['id']}: {request['text']}"})]
        else:
            message = (f"Hello {request['client']},\n\nWe could not match your request to a tracked issue. "
                       "Please confirm the expected behavior so the team can triage it.\n\n"
                       f"Your request: {request['text']}\n")
            actions = [("github.issue_create", {"title": f"Client request {request['id']}",
                                               "body": request["text"]})]
        actions.append(("email.send", {"to": request["email"], "subject": "Weekly request follow-up", "body": message}))
        for action, payload in actions:
            digest = hashlib.sha256(json.dumps({"action": action, "payload": payload},
                                              sort_keys=True).encode()).hexdigest()
            proposals.append({"id": digest, "request_id": request["id"], "action": action,
                              "payload": payload, "approval": "required", "executed": False})
    return {"created_at": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "fixture_notice": "Synthetic fixture replay; not a model benchmark" if mode == "replay" else None,
            "threshold": threshold, "matches": matches, "proposals": proposals,
            "summary": {"requests": len(requests), "reviewed": sum(m["reviewed"] for m in matches),
                        "review_changed": sum(m["reviewed"] and m["initial_choice"] != m["choice"] for m in matches),
                        "unmatched": sum(m["choice"] == NO_MATCH for m in matches),
                        "external_writes_executed": 0}}


def save(result, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents an unattended rerun from overwriting reviewed drafts.
    with (output / "followup.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return output / "followup.json"

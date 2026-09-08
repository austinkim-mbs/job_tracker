from config import TERMINAL_STATUSES

# Maps email event_type → status, in priority order
# Higher in the list = higher priority (won't downgrade)
STATUS_PRIORITY = [
    "Accepted",
    "Rejected",
    "Offer",
    "Interviewing",
    "Screening",
    "Applied",
]

EVENT_TO_STATUS = {
    "application_received": "Applied",
    "recruiter_outreach": "Screening",
    "phone_screen": "Screening",
    "interview_scheduled": "Interviewing",
    "technical_assessment": "Interviewing",
    "offer": "Offer",
    "rejection": "Rejected",
}


def _priority(status: str) -> int:
    try:
        return STATUS_PRIORITY.index(status)
    except ValueError:
        return len(STATUS_PRIORITY)


class StateMachine:
    def transition(self, current_status: str | None, event_type: str) -> str:
        """
        Return the new status for an application given its current status
        and the event type of the incoming email.

        Never downgrades (e.g., an application_received email won't move
        an application from Interviewing back to Applied).
        Never transitions out of terminal statuses.
        """
        if current_status in TERMINAL_STATUSES:
            return current_status

        new_status = EVENT_TO_STATUS.get(event_type)
        if new_status is None:
            return current_status or "Applied"

        if current_status is None:
            return new_status

        # Keep whichever status is higher priority (lower index = higher priority)
        if _priority(new_status) <= _priority(current_status):
            return new_status
        return current_status

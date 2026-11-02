"""Durable markers for submissions that must be reviewed before retrying."""

NEEDS_REVIEW = "NEEDS_REVIEW:"
PUBLISH_PENDING = (
    f"{NEEDS_REVIEW} TikTok submission started; confirmation pending. "
    "Check TikTok before using Force TikTok to retry."
)


def needs_review(message: object) -> bool:
    return str(message).startswith(NEEDS_REVIEW)

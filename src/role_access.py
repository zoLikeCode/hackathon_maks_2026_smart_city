from __future__ import annotations

from src.verification_store import PhoneVerificationRepository


ROLE_SWITCH_PHONE = "+79872660500"
ROLE_START_PARAMS = {
    "role_specialist": "specialist",
    "role_chairman": "chairman",
    "role_owner": "owner",
}


def has_role_switch_access(
    phone_store: PhoneVerificationRepository | None,
    user_id: int,
) -> bool:
    """Grant the role menu only to a MAX user with a signed, stored contact."""
    verification = phone_store.get(user_id) if phone_store is not None else None
    return verification is not None and verification.phone == ROLE_SWITCH_PHONE

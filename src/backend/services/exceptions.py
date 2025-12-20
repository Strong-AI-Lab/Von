from typing import List


class MultipleUsersForEmailError(Exception):
    """Raised when multiple users are found for a single email address."""

    def __init__(self, email: str, user_ids: List[str]):
        self.email = email
        self.user_ids = user_ids
        super().__init__(f"Multiple users found for email '{email}': {user_ids}")

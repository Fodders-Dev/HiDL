"""Profile-mode helpers shared by handlers and framework-free services."""

def is_student_mode(user) -> bool:
    """Return whether a user mapping represents the student profile."""
    if user is None:
        return False
    try:
        return user.get("profile_mode", "adult") == "student"
    except AttributeError:
        return False

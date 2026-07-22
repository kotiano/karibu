"""Password strength checks.

Length alone doesn't stop credential-stuffing: "password123" satisfies an
8-char minimum and is in every attacker's first hundred guesses. This module
rejects the passwords that appear at the top of real breach corpora
(rockyou/HIBP-style lists), normalized to lowercase.

Kept as a small embedded set — big enough to kill the guesses automated tools
try first, small enough to cost nothing at runtime.
"""

# Top of the real-world guess lists, lowercased. Exact-match after lowering.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "p@ssw0rd",
        "12345678", "123456789", "1234567890", "87654321", "11111111",
        "00000000", "12341234", "12344321", "123123123", "1q2w3e4r",
        "1qaz2wsx", "qwerty123", "qwertyuiop", "asdfghjkl", "zxcvbnm1",
        "abcd1234", "abc12345", "a1b2c3d4", "qwer1234", "letmein1",
        "welcome1", "sunshine", "iloveyou", "princess", "football",
        "baseball", "superman", "trustno1", "dragon12", "monkey12",
        "shadow12", "master12", "hello123", "freedom1", "whatever",
        "michael1", "jennifer", "computer", "internet", "samsung1",
        "liverpool", "arsenal1", "chelsea1", "manchester", "starwars",
        "pokemon1", "naruto12", "minecraft", "roblox123", "fortnite",
        "admin123", "administrator", "root1234", "user1234", "test1234",
        "demo1234", "guest123", "changeme", "default1", "secret12",
        "kenya123", "nairobi1", "mombasa1", "safaricom", "mpesa123",
        "karibu123", "jambo123", "hakuna123", "kenya2024", "kenya2025",
        "aaaaaaaa", "qqqqqqqq", "11223344", "10203040", "19871987",
        "19901990", "19951995", "20002000", "20102010", "20202020",
    }
)


def password_problem(password: str) -> str | None:
    """Return a human-readable problem with the password, or None if OK.

    The 8-char minimum is enforced by the Pydantic schema; this adds the
    quality checks on top.
    """
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        return "That password is too common — pick something less guessable."
    # All one repeated character ("aaaaaaaa"-style beyond the list above).
    if len(set(lowered)) == 1:
        return "Password can't be a single repeated character."
    # Purely sequential digits of any length ("123456789012").
    if lowered.isdigit():
        digits = [int(c) for c in lowered]
        diffs = {b - a for a, b in zip(digits, digits[1:])}
        if diffs <= {1} or diffs <= {-1} or diffs <= {0}:
            return "Password can't be a simple number sequence."
    return None

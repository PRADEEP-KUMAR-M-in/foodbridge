"""All the login stuff with zero extra libraries: scrambled (hashed) passwords, signed login tokens that expire,
and a timeout when someone keeps guessing wrong. Basically the bouncer."""
# Only standard-library tools here, that's the "zero extra libraries" part.
import base64, hashlib, hmac, json, os, secrets, time

# The secret key used to sign login tokens and make pickup codes.
# Set the FOODBRIDGE_SECRET env var for real use, otherwise it's random on every restart (and everyone gets logged out).
SECRET = os.getenv("FOODBRIDGE_SECRET") or secrets.token_hex(32)
# A login token stops working after 12 hours.
TOKEN_LIFETIME_SECONDS = 12 * 3600
# How many times we scramble the password; more rounds = slower for hackers (and a tiny bit slower for us).
PASSWORD_HASH_ROUNDS = 100_000
# Remembers when each username last failed to log in (kept in memory, so it resets when the server restarts).
_FAILED_LOGINS: dict[str, list[float]] = {}
# Allow 5 wrong tries inside a 60 second window, then lock that username out for a bit.
MAX_FAILED_LOGINS, LOCKOUT_WINDOW_SECONDS = 5, 60


def hash_password(password: str) -> str:
    # A fresh random "salt" so two people with the same password still get different hashes.
    salt = secrets.token_bytes(16)
    # Store the salt and the scrambled password together, separated by a $ sign.
    return salt.hex() + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_HASH_ROUNDS).hex()


# A throwaway hash we check against when the username doesn't exist, so a wrong username takes as long as a wrong password.
DUMMY_HASH = hash_password("dummy-password-for-timing")


def check_password(password: str, stored_hash: str) -> bool:
    try:
        # Split the saved value back into the salt and the expected scrambled password.
        salt_text, expected_hash = stored_hash.split("$")
        # Scramble the typed password with the same salt and the same number of rounds.
        computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_text), PASSWORD_HASH_ROUNDS).hex()
        # compare_digest compares in a way that doesn't leak how many characters matched.
        return hmac.compare_digest(computed_hash, expected_hash)
    except Exception:
        # Anything weird (broken hash, bad format) just counts as "wrong password".
        return False


def _sign(payload_b64: str) -> str:
    # Makes a signature that proves the token was created by us (only someone with SECRET can make it).
    return hmac.new(SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def make_token(org_id: int) -> str:
    # The token's contents: which organisation ("o") and when it expires ("e"), turned into URL-safe text.
    payload_b64 = base64.urlsafe_b64encode(json.dumps({"o": org_id, "e": int(time.time()) + TOKEN_LIFETIME_SECONDS}).encode()).decode().rstrip("=")
    # Token = contents + a dot + signature.
    return payload_b64 + "." + _sign(payload_b64)


def read_token(token: str) -> int | None:
    try:
        # Split the token into its two halves.
        payload_b64, signature = token.split(".")
        # If the signature doesn't match, someone tampered with it: reject.
        if not hmac.compare_digest(signature, _sign(payload_b64)):
            return None
        # Put back the "=" padding we removed earlier, then decode the contents.
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        # Give back the organisation id, but only if the token hasn't expired yet.
        return data["o"] if data["e"] > time.time() else None
    except Exception:
        # Garbage in, nothing out.
        return None


def is_locked_out(user: str) -> bool:
    now = time.time()
    # Forget failed attempts older than the lockout window, keep only the recent ones.
    _FAILED_LOGINS[user] = [attempt_time for attempt_time in _FAILED_LOGINS.get(user, []) if now - attempt_time < LOCKOUT_WINDOW_SECONDS]
    # Locked out if there are too many recent failures.
    return len(_FAILED_LOGINS[user]) >= MAX_FAILED_LOGINS


def record_failed_login(user: str):
    # Add "right now" to this username's list of failures.
    _FAILED_LOGINS.setdefault(user, []).append(time.time())


def forget_failed_logins(user: str):
    # After a successful login, wipe their failure history (the None means "don't crash if there's nothing to remove").
    _FAILED_LOGINS.pop(user, None)

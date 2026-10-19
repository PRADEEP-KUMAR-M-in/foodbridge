"""Speak-to-List v1: a rule-based parser (pattern matching, NOT an AI model, no cap).
Takes a sentence like "20 packs of bread, packed at 6 pm, collect before 8 pm" and turns it into a draft listing.
Safety stuff is never guessed: storage is only filled in if the donor actually said it, and the donor always has to confirm storage + packed time.
The output shape stays the same, so an AI model can replace parse_listing() later without touching the safety checks."""
import re  # regular expressions: the pattern-matching engine this whole file runs on
import unicodedata  # to normalise Tamil/other text so the same letters always look identical to the code
from datetime import datetime, timedelta  # date+time and lengths of time

# Shown in the output so everyone can see this is rules, not an AI model.
ENGINE = "rules-v1 (pattern matching, not a language model)"
# Number words we understand (1 to 19).
ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
# The "tens" words (20 to 90).
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

# Words that tell us what kind of food it is (English and Tamil).
CATEGORY_WORDS = {
    "packaged_bakery": ["bread", "bun", "buns", "biscuit", "biscuits", "cake", "cakes", "rusk", "loaf", "loaves", "bakery", "pastry",
                        "pastries", "cookies", "ரொட்டி", "பன்", "பிஸ்கட்", "கேக்", "பேக்கரி", "ரஸ்க்"],
    "cooked_meal": ["meal", "meals", "rice", "biryani", "sambar", "curry", "cooked", "dal", "idli", "dosa",
                    "சாதம்", "சோறு", "பிரியாணி", "சாம்பார்", "குழம்பு", "சமைத்த"],
}
# Words that count as a "unit" after a number, in English...
UNITS_EN = r"packs?|packets?|boxes|box|meals?|portions?|plates?|loaves|loaf|pieces?|pcs|trays?|cartons?|bags?|kgs?|litres?|liters?"
# ...and in Tamil.
UNITS_TA = r"பாக்கெட்|பொட்டலம்|பொதி|பெட்டி|பிளேட்"
# Phrases that tell us how the food is stored.
STORAGE_PATTERNS = [   # order matters: "not refrigerated" has to be checked before "refrigerat"
    ("ambient", r"room temp|ambient|normal temp|not refrigerated|no fridge|அறை வெப்பநிலை|சாதாரண வெப்பநிலை"),
    ("chilled", r"fridge|refrigerat|chilled|\bcold\b|cool box|cooler|குளிர்சாதன|ஃப்ரிட்ஜ்|பிரிட்ஜ்"),
    ("hot_held", r"\bhot\b|piping|steaming|\bwarm\b|சூடாக|சூடு"),
]
# Plain-English warnings the parser can attach to its answer.
NOTES = {
    "MIXED_CATEGORIES": "Different food types found in one sentence. Please make separate listings.",
    "CONFLICTING_STORAGE": "More than one storage type mentioned. Please choose the correct one.",
    "UNIT_NOT_PACKS": "Quantity is not in packs; it is used as a unit count. Please check it.",
    "DEADLINE_PASSED": "The collection time you said is already in the past. Please check it.",
    "AMBIGUOUS_CLOCK": "A clock time had no am/pm, so the nearest likely time was used. Please check it.",
    "COMBO_NOT_ACCEPTED": "This food type and storage combination is not accepted yet.",
}


def words_to_digits(text: str) -> str:
    # "a hundred" -> 100
    text = re.sub(r"\ba hundred\b", "100", text)
    # "half an hour" -> "30 minutes"
    text = re.sub(r"\bhalf an? hour\b", "30 minutes", text)
    # "an hour" / "a hour" -> "1 hour"
    text = re.sub(r"\ban? hour\b", "1 hour", text)
    # Two-word numbers like "twenty five" or "thirty-two" become digits (25, 32).
    text = re.sub(r"\b(" + "|".join(TENS) + r")[ -](" + "|".join(k for k in ONES if ONES[k] < 10) + r")\b",
               lambda m: str(TENS[m[1]] + ONES[m[2]]), text)
    # All the single number words together in one lookup.
    all_number_words = {**ONES, **TENS, "hundred": 100}
    # Replace any remaining number word with its digits (longest words first so "seventeen" isn't mistaken for "seven").
    return re.sub(r"\b(" + "|".join(sorted(all_number_words, key=len, reverse=True)) + r")\b", lambda m: str(all_number_words[m[1]]), text)


def _to_minutes(n: int, unit: str) -> int:
    # If the unit is hours (English "h..." or Tamil "மணி..."), multiply by 60; otherwise it's already minutes.
    return n * 60 if unit.startswith("h") or unit.startswith("மணி") else n


def _guess_clock_time(hour: int, minute: int, am_pm: str | None, now_local: datetime, mode: str):
    """mode 'past': the latest matching time that's not after now. mode 'future': the earliest matching time after now."""
    # Impossible clock values: give up.
    if minute > 59 or hour > 24:
        return None, False
    if am_pm:
        # They said am or pm, so there's exactly one possible hour (24-hour style), and it's not ambiguous.
        candidates, ambiguous = [hour % 12 + (12 if am_pm == "pm" else 0)], False
    elif hour >= 13 or hour == 0:
        # Hours like 15 or 0 can only be 24-hour time, so no ambiguity.
        candidates, ambiguous = [hour % 24], False
    else:
        # Something like "6" could be morning or evening, so try both and flag it as ambiguous.
        candidates, ambiguous = [hour % 12, hour % 12 + 12], True
    # Build real date+times for yesterday, today and tomorrow at each candidate hour.
    possible_times = [datetime(now_local.year, now_local.month, now_local.day, c % 24, minute) + timedelta(days=d) for c in candidates for d in (-1, 0, 1)]
    # "past" keeps times up to now; "future" keeps times in the next 12 hours.
    options = [t for t in possible_times if t <= now_local] if mode == "past" else [t for t in possible_times if now_local < t <= now_local + timedelta(hours=12)]
    # Nothing fits: no answer.
    if not options:
        return None, False
    # Pick the most recent past time, or the soonest future time.
    return (max(options) if mode == "past" else min(options)), ambiguous


def parse_listing(text: str, now: datetime, tz_offset_minutes: int = 0, rules: dict | None = None) -> dict:
    """now: UTC time (no timezone tag). tz_offset_minutes: the donor's phone timezone from JavaScript's getTimezoneOffset() (India = -330)."""
    # Clean up the text (same letters always encoded the same way, no spaces at the ends).
    raw = unicodedata.normalize("NFC", text).strip()
    # Lowercase it and swap number words for digits so patterns only have to look for digits.
    text_lower = words_to_digits(raw.lower())
    # The donor's local time (JavaScript's offset is minus for places ahead of UTC, so subtracting works).
    now_local = now - timedelta(minutes=tz_offset_minutes)
    # confidence = how sure we are of each field; notes = warnings to show the donor.
    confidence, notes = {}, []

    # step 1: what kind of food is it?
    # Collect every food category that has at least one of its words in the sentence (ASCII words need word boundaries; Tamil words don't).
    found = {c for c, ws in CATEGORY_WORDS.items() for w in ws if re.search((r"\b" + re.escape(w) + r"\b") if w.isascii() else re.escape(w), text_lower)}
    category = None
    if len(found) == 1:
        # Exactly one type found: use it.
        category = next(iter(found))
        confidence["category"] = 0.9
    elif len(found) > 1:
        # More than one type: don't guess, warn instead.
        notes.append("MIXED_CATEGORIES")

    # step 2: how is it stored? (only if they actually said it)
    storages_found, leftover_text = [], text_lower
    for storage_name, pattern in STORAGE_PATTERNS:
        if re.search(pattern, leftover_text):
            # Remember this storage type...
            storages_found.append(storage_name)
            # ...and cut the match out so "not refrigerated" doesn't ALSO count as "refrigerated".
            leftover_text = re.sub(pattern, " ", leftover_text)
    storage = None
    if len(storages_found) == 1:
        # Exactly one storage type mentioned: use it.
        storage = storages_found[0]
        confidence["storage"] = 0.9
    elif len(storages_found) > 1:
        # Contradicting storage words: don't guess, warn instead.
        notes.append("CONFLICTING_STORAGE")

    # step 3: how much is there?
    quantity, unit = None, None
    # Every food word in one list, then joined into a pattern (longest first so the longest word matches first).
    all_words = [w for ws in CATEGORY_WORDS.values() for w in ws]
    category_pattern = "|".join(re.escape(w) for w in sorted(all_words, key=len, reverse=True))
    # Try three patterns from strictest to loosest: "20 packs", "20 big packs" (up to 2 words in between), "20 bread".
    for pat in (rf"(\d+)\s*(?:x\s*)?(\b(?:{UNITS_EN})\b|{UNITS_TA})",
                rf"(\d+)\s*(?:\S+\s+){{0,2}}?(\b(?:{UNITS_EN})\b|{UNITS_TA})",
                rf"(\d+)\s*({category_pattern})"):
        match = re.search(pat, text_lower)
        if match:
            # First pattern that matches wins: the number and the unit word.
            quantity, unit = int(match[1]), match[2]
            confidence["quantity"] = 0.9
            break
    if unit and re.fullmatch(r"kgs?|litres?|liters?", unit):
        # Weight or volume isn't a pack count, so ask the donor to double-check.
        notes.append("UNIT_NOT_PACKS")

    # step 4: when was it packed?
    prepared, clock_ambiguous = None, False
    # First try "20 minutes ago" style in English, then the Tamil "... முன்" style.
    match = (re.search(r"(\d+)\s*(minutes?|mins?|hours?|hrs?)\s*(?:ago|back)", text_lower)
         or re.search(r"(\d+)\s*(நிமிடங்களுக்கு|நிமிடத்திற்கு|நிமிடம்|மணி நேரத்திற்கு|மணி நேரம்|மணிநேரம்|மணி நேரத்துக்கு)\s*முன்", text_lower))
    if match:
        # "N minutes/hours ago": subtract that from the current time.
        prepared = now - timedelta(minutes=_to_minutes(int(match[1]), match[2]))
        confidence["prepared_at"] = 0.9
    else:
        # Otherwise look for a clock time like "packed at 6 pm"; the "tail" stops us reading "6 packs" as a time.
        tail = r"(?!\d)(?!\s*(?:packs?|packets?|boxes|meals?|kgs?|minutes?|mins?|hours?|hrs?))"
        match = (re.search(r"\b(?:packed|made|cooked|prepared|baked|ready)\s*(?:it\s*)?(?:at|around|from)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?" + tail, text_lower)
             or re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:packed|made|cooked|prepared|baked)", text_lower))
        if match:
            # Turn the clock time into a real moment in the past (and note if am/pm was missing).
            clock_time, is_ambiguous = _guess_clock_time(int(match[1]), int(match[2] or 0), match[3], now_local, "past")
            if clock_time:
                # Convert the donor's local clock time back to UTC.
                prepared = clock_time + timedelta(minutes=tz_offset_minutes)
                clock_ambiguous |= is_ambiguous
                # Less sure if we had to guess am/pm.
                confidence["prepared_at"] = 0.5 if is_ambiguous else 0.8

    # step 5: pickup deadline (if they say a few, the earliest one wins, safer that way)
    candidates = []
    # "within 2 hours" style in English, then the Tamil version.
    match = (re.search(r"\b(?:within|in|next)\s*(\d+)\s*(minutes?|mins?|hours?|hrs?)", text_lower)
         or re.search(r"(\d+)\s*(மணி நேரத்தில்|மணி நேரத்திற்குள்|மணி நேரத்துக்குள்|மணிநேரத்தில்|நிமிடங்களில்|நிமிடத்தில்|நிமிடங்களுக்குள்)", text_lower))
    if match:
        unit_w = match[2]
        # Work out if the unit was hours, minutes or an English word, add that to "now", and note we're fairly sure (0.9).
        candidates.append((now + timedelta(minutes=_to_minutes(int(match[1]), "h" if "மணி" in unit_w else ("m" if "நிமிட" in unit_w else unit_w))), 0.9))
    # "before 8 pm" style (also by / until / till / latest by).
    match = re.search(r"\b(?:before|by|until|till|latest by)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?!\d)(?!\s*(?:packs?|packets?|boxes|meals?|kgs?|minutes?|mins?|hours?|hrs?))", text_lower)
    if match:
        # Turn the clock time into the next matching moment in the future.
        clock_time, is_ambiguous = _guess_clock_time(int(match[1]), int(match[2] or 0), match[3], now_local, "future")
        if clock_time:
            # Add it as a candidate deadline (converted back to UTC).
            candidates.append((clock_time + timedelta(minutes=tz_offset_minutes), 0.5 if is_ambiguous else 0.8))
            clock_ambiguous |= is_ambiguous
        else:
            notes.append("DEADLINE_PASSED")       # that time doesn't happen in the next 12 hours, so it's already gone
    deadline = None
    if candidates:
        # If several deadlines were mentioned, the earliest one wins.
        deadline, deadline_confidence = min(candidates, key=lambda x: x[0])
        confidence["donor_deadline"] = deadline_confidence
    if clock_ambiguous:
        # At least one time had no am/pm, so tell the donor to double-check.
        notes.append("AMBIGUOUS_CLOCK")

    # How many minutes ago it was packed, and how many minutes from now until the deadline.
    packed_ago = max(0, round((now - prepared).total_seconds() / 60)) if prepared else None
    within = round((deadline - now).total_seconds() / 60) if deadline else None
    if within is not None and within <= 0:
        # A deadline that's already passed is useless: drop it and warn.
        notes.append("DEADLINE_PASSED")
        within = None
        deadline = None
        confidence.pop("donor_deadline", None)

    # Only check the food-type + storage combination if we were given the rules and know both values.
    combo = None
    if rules is not None and category and storage:
        # True if the rules accept this combination.
        combo = rules.get((category, storage)) is not None
        if not combo:
            notes.append("COMBO_NOT_ACCEPTED")

    # Helper: turn a date+time into text ending in Z (UTC), or None if there isn't one.
    iso = lambda d: d.isoformat() + "Z" if d else None
    # The fields the donor's form will pre-fill.
    fields = {"category": category, "storage": storage, "quantity": quantity, "packed_ago_min": packed_ago, "collect_within_min": within}
    # Bundle everything into the final answer.
    return {"engine": ENGINE, **fields, "unit": unit, "prepared_at": iso(prepared), "donor_deadline": iso(deadline),
            "accepted_combo": combo, "field_confidence": confidence,
            "needs_confirmation": ["storage", "prepared_at"],       # the donor ALWAYS confirms the safety stuff, no skipping
            "missing": [k for k, v in fields.items() if v is None], "notes": notes}

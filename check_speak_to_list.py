"""How accurate the Speak-to-List parser is, field by field:  python check_speak_to_list.py
HEADS UP: the parser was written while looking at these exact sentences, so these numbers look better than they really are.
To measure it honestly, test on fresh sentences collected from real donors before quoting any accuracy."""
from datetime import datetime  # for the fixed "pretend now" below
from app.speak_to_list import parse_listing  # the parser we're testing
from app.safe_window import load_rules  # the rules, so the parser can check food + storage combos
from tests.speak_to_list_cases import CASES, FIELDS  # the test sentences, and which fields we compare

# A fixed "now" (13:30 UTC) and India's timezone offset (-330 minutes), so the results are identical every run.
NOW, TIMEZONE_OFFSET_MIN = datetime(2026, 10, 17, 13, 30), -330
# right = how many sentences got each field correct; misses = a list of every mistake.
right = {field: 0 for field in FIELDS}
misses = []
# Go through every test sentence; "expected" is the list of correct answers that follows the sentence.
for text, *expected in CASES:
    # Ask the parser what it thinks.
    actual = parse_listing(text, NOW, TIMEZONE_OFFSET_MIN, load_rules())
    # Compare field by field with the correct answers.
    for field, expected_value in zip(FIELDS, expected):
        if actual[field] == expected_value:
            # Correct: add one to that field's score.
            right[field] += 1
        else:
            # Wrong: remember the first 60 characters of the sentence, the field, what we expected and what we got.
            misses.append((text[:60], field, expected_value, actual[field]))
# How many sentences there were.
total = len(CASES)
print(f"{total} sentences")
# One line per field with its score and percentage (the // rounds down).
for field in FIELDS:
    print(f"  {field:20s} {right[field]}/{total}  ({100*right[field]//total}%)")
# Then list every mistake so it's easy to see what to fix.
for miss in misses:
    print("  MISS", miss)

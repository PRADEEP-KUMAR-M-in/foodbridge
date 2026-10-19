"""Made-up test sentences written by the developer (NOT real donor speech).
now = 2026-10-17 13:30 UTC, device offset -330 (India time) => local time 19:00.
Fields: (text, category, storage, quantity, packed_ago_min, collect_within_min)"""
# Short names for the two food types so the table below stays readable (B = bakery, C = cooked meal).
B, C = "packaged_bakery", "cooked_meal"
# Each row: the sentence, then the correct answers in the same order as FIELDS below.
CASES = [
    ("20 packs of bread, packed at 6 pm, collect before 8 pm, room temperature", B, "ambient", 20, 60, 60),
    ("We have 15 boxes of buns packed 30 minutes ago, please collect within 2 hours", B, None, 15, 30, 120),
    ("30 cooked meals, kept hot, made 45 minutes ago, pickup within 90 minutes", C, "hot_held", 30, 45, 90),
    ("twenty five packets of biscuits, packed 1 hour ago, collect within 3 hours, ambient", B, "ambient", 25, 60, 180),
    ("10 loaves of bread in the fridge packed 2 hours ago collect by 9 pm", B, "chilled", 10, 120, 120),
    ("40 rice meals hot packed at 6:30 pm pick up before 8:30 pm", C, "hot_held", 40, 30, 90),
    ("12 cakes packed 20 mins ago, collect in 1 hour, room temperature", B, "ambient", 12, 20, 60),
    ("50 biryani packets kept warm, cooked 1 hour ago, within 2 hours", C, "hot_held", 50, 60, 120),
    ("8 packs of rusk, packed at 5 pm, collect before 9 pm", B, None, 8, 120, 120),
    ("100 portions of sambar rice packed 15 minutes ago kept hot collect within 1 hour", C, "hot_held", 100, 15, 60),
    ("five boxes of bread packed half an hour ago collect within an hour ambient", B, "ambient", 5, 30, 60),
    ("35 packs of buns chilled, packed 3 hours ago, collect before 10 pm", B, "chilled", 35, 180, 180),
    ("18 meals cooked 2 hours ago, hot, pickup within 30 minutes", C, "hot_held", 18, 120, 30),
    ("22 cake boxes packed at 7 pm collect by 9 pm room temperature", B, "ambient", 22, 0, 120),
    ("60 bread packets, packed an hour ago, collect within 4 hours, in a cool box refrigerated", B, "chilled", 60, 60, 240),
    ("7 meal packs hot, made 10 minutes ago, collect within 45 minutes", C, "hot_held", 7, 10, 45),
    ("a hundred buns packed 1 hour ago collect within 2 hours", B, None, 100, 60, 120),
    ("9 trays of cooked rice kept hot, made 20 minutes ago, collect within 40 minutes", C, "hot_held", 9, 20, 40),
    ("14 packs of bread not refrigerated, packed 10 minutes ago, within 3 hours", B, "ambient", 14, 10, 180),
    ("thirty two packets of cookies packed at 5:45 pm collect before 9 pm", B, None, 32, 75, 120),
    # Tamil (only partly covered; a native speaker needs to check this with real speech)
    ("20 பாக்கெட் ரொட்டி, 30 நிமிடங்களுக்கு முன் பேக் செய்தது, 2 மணி நேரத்தில் எடுத்துக்கொள்ளலாம்", B, None, 20, 30, 120),
    ("40 சாதம் பொட்டலம் சூடாக, 1 மணி நேரத்திற்கு முன் சமைத்தது, 3 மணி நேரத்திற்குள் எடுக்கவும்", C, "hot_held", 40, 60, 180),
    ("15 பன் பாக்கெட் அறை வெப்பநிலை 15 நிமிடங்களுக்கு முன் பேக் செய்தது 1 மணி நேரத்தில்", B, "ambient", 15, 15, 60),
    ("10 கேக் பெட்டி குளிர்சாதனப் பெட்டியில், 2 மணி நேரத்திற்கு முன் தயாரித்தது, 90 நிமிடங்களில் எடுக்கவும்", B, "chilled", 10, 120, 90),
    ("25 பிரியாணி பொட்டலம் சூடாக 30 நிமிடங்களுக்கு முன் சமைத்தது 1 மணி நேரத்தில்", C, "hot_held", 25, 30, 60),
    ("8 பிஸ்கட் பாக்கெட் 3 மணி நேரத்திற்குள்", B, None, 8, None, 180),
]
# The names of the answer columns, in order.
FIELDS = ["category", "storage", "quantity", "packed_ago_min", "collect_within_min"]

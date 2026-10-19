"""Makes the simulation evidence:   python run_simulation.py [runs]
Writes reports/SIMULATION_REPORT.md and, if matplotlib is installed, reports/sim_headline.png and reports/sim_window_sweep.png.
EVERYTHING HERE IS A MODEL under stated assumptions, not a real-world result."""
# os = make folders and read file paths
import os
# sys = read the optional "number of runs" typed after the command
import sys
# asdict = settings to dictionary, replace = copy settings with one change
from dataclasses import asdict, replace
# the simulator pieces: the two main setups (BASELINE = today, FULL = FOODBRIDGE), their names, the settings class, and the run/summary functions
from app.simulator import BASELINE, FULL, LABELS, Params, run_many, simulate, summarise

# How many simulated weeks to run: whatever you typed after the command, otherwise 200.
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
# Fixed random seed, so re-running gives identical numbers.
SEED = 1
# The default assumptions (see Params in app/simulator.py).
params = Params()
# Make sure the reports/ folder exists.
os.makedirs("reports", exist_ok=True)

# FOODBRIDGE with the first-dibs cascade switched off.
BROADCAST = replace(FULL, cascade=False)       # filters + instant claim + auto expiry, but everyone eligible hears about it at once


# Runs many simulated weeks for one setup and boils them down to median + 5th/95th percentile.
def summarise_runs(run_params, flags, salt=0):
    return summarise(run_many(run_params, flags, RUNS, SEED, salt))


# Formatting helpers: 0.5 -> "50%", minutes -> whole numbers ("—" if missing), numbers -> 1 decimal.
as_percent = lambda v: f"{100 * v:.0f}%"
as_minutes = lambda v: "—" if v is None else f"{v:.0f}"
one_decimal = lambda v: f"{v:.1f}"


# Shows one statistic as "median (low to high)".
def cell_text(stat, formatter):
    return f"{formatter(stat['median'])} ({formatter(stat['p5'])} to {formatter(stat['p95'])})"


# Builds one row of the markdown results table for a given process.
def table_row(name, stats):
    return (f"| {name} | {cell_text(stats['completion'], as_percent)} | {cell_text(stats['median_time'], as_minutes)} | {cell_text(stats['wasted_per_100'], one_decimal)} | {cell_text(stats['top_share'], as_percent)} |")


# The header row shared by every results table.
TABLE_HEADER = "| Process | Share of listed food delivered | Minutes to confirmed claim | Wasted trips per 100 listings | Largest organisation's share |\n|---|---|---|---|---|"

# The main comparison: every scenario, run RUNS times.
main = simulate(params, runs=RUNS, seed=SEED)
# Split the answer into the scenario results and the week-by-week win counts.
scenarios, paired_counts = main["scenarios"], main["paired"]
# Pull out the two headline setups.
today, foodbridge = scenarios["baseline"], scenarios["full"]
# Also run FOODBRIDGE without the first-dibs window, to compare.
broadcast_only = summarise_runs(params, BROADCAST, salt=11)

# ---- window sweep (trying different first-dibs windows)
# The first-dibs window lengths (in minutes) we try.
WINDOWS = [1, 2, 3, 5, 10, 15]
# Results for broadcasting, then each window length.
sweep = [("broadcast to eligible", summarise_runs(params, BROADCAST, salt=21))] + [(f"first dibs {w} min", summarise_runs(replace(params, offer_window_min=w), FULL, salt=21)) for w in WINDOWS]

# ---- what-if check (change one assumption at a time)
# Each assumption we wiggle one at a time: (setting name, label, values to try).
WHAT_IF_SETTINGS = [("p_respond", "Chance an organisation answers", [0.4, 0.7, 0.9]),
        ("median_delay_min", "Median minutes to answer", [10, 20, 40]),
        ("p_noshow_mean", "Average no-show chance", [0.05, 0.12, 0.25]),
        ("p_blind_yes", "Says yes to a misfit broadcast", [0.05, 0.30, 0.50]),
        ("recipients", "Receiving organisations", [4, 8, 16]),
        ("deadline_choices", "Safe window left at posting", [(45, 60, 90), (90, 120, 180, 240), (180, 240, 360)])]
# Table rows for the sensitivity section, filled in below.
sens_rows = []
# Go through every assumption and every value we want to try for it.
for key, label, vals in WHAT_IF_SETTINGS:
    # Change just this one assumption.
    for v in vals:
        # Copy the default settings with that one value changed.
        varied_params = replace(params, **{key: v})
        # Run both processes with the changed setting.
        today_stats, foodbridge_stats = summarise_runs(varied_params, BASELINE, 31), summarise_runs(varied_params, FULL, 32)
        # How the value is written in the table.
        shown = "/".join(map(str, v)) + " min" if key == "deadline_choices" else v
        # Add the table row for this setting.
        sens_rows.append(f"| {label} = {shown} | {as_percent(today_stats['completion']['median'])} | {as_percent(foodbridge_stats['completion']['median'])} | {as_minutes(today_stats['median_time']['median'])} | {as_minutes(foodbridge_stats['median_time']['median'])} | {one_decimal(today_stats['wasted_per_100']['median'])} | {one_decimal(foodbridge_stats['wasted_per_100']['median'])} |")

# ---- a world that's nice to the old way (to be fair to it)
# A kinder world for the old way: fewer misfit replies, fast confirmation, few no-shows.
friendly_params = replace(params, p_blind_yes=0.05, confirm_delay_min=1, p_noshow_mean=0.05)
# Run both processes in that friendlier world.
friendly_today, friendly_foodbridge = summarise_runs(friendly_params, BASELINE, 41), summarise_runs(friendly_params, FULL, 42)

# ---- findings, worked out from the numbers above (no claims beyond them)
# The first-dibs setting that gave the fastest claims.
best_w = min(sweep, key=lambda x: x[1]["median_time"]["median"])
# The longest window we tested.
longest = sweep[-1]
# The bullet-point findings, written from the numbers above (nothing beyond them).
findings = [
    f"Wasted trips per 100 listings: {one_decimal(today['wasted_per_100']['median'])} with today's process versus {one_decimal(foodbridge['wasted_per_100']['median'])} with FOODBRIDGE.",
    f"Share of listed food delivered (median of {RUNS} simulated weeks): {as_percent(today['completion']['median'])} today versus {as_percent(foodbridge['completion']['median'])} with FOODBRIDGE. Week by week FOODBRIDGE was higher in {paired_counts['full_better']}, equal in {paired_counts['tie']} and lower in {paired_counts['full_worse']} of {paired_counts['runs']} weeks, so this model does NOT show that FOODBRIDGE rescues more food.",
    f"Median minutes to a confirmed claim: {as_minutes(today['median_time']['median'])} today versus {as_minutes(foodbridge['median_time']['median'])} with FOODBRIDGE using a {params.offer_window_min:.0f}-minute first-dibs window; broadcasting to eligible organisations only took {as_minutes(broadcast_only['median_time']['median'])}.",
    f"Window sweep: the fastest setting tested was '{best_w[0]}' ({as_minutes(best_w[1]['median_time']['median'])} min to a claim); the longest window tested, '{longest[0]}', took {as_minutes(longest[1]['median_time']['median'])} min and delivered {as_percent(longest[1]['completion']['median'])}.",
    f"Largest organisation's share of delivered food: {as_percent(today['top_share']['median'])} today versus {as_percent(foodbridge['top_share']['median'])} with FOODBRIDGE.",
    f"In a world friendlier to today's process (fewer misfit replies, fast confirmation, few no-shows) the delivered share was {as_percent(friendly_today['completion']['median'])} today versus {as_percent(friendly_foodbridge['completion']['median'])} with FOODBRIDGE, and minutes to a claim were {as_minutes(friendly_today['median_time']['median'])} versus {as_minutes(friendly_foodbridge['median_time']['median'])}.",
]

# The settings as a dictionary, for the assumptions table.
assumptions = asdict(params)
# The entire markdown report, with live numbers dropped in.
report_markdown = f"""# FOODBRIDGE simulation report

> **SIMULATION, NOT A MEASUREMENT.** Every probability and delay below is an assumption chosen by the developers. The results show what the design could do if the assumptions held. Only a real pilot can show what it does. Generated by `python run_simulation.py {RUNS}` (seed {SEED}); re-running gives identical numbers.

## What is compared
- **Today:** a donor broadcasts to every organisation; the first "yes" gets it; a confirmation call follows; a no-show is noticed only after a manual wait; misfits (wrong storage or capacity) are often found late, so trips are wasted.
- **FOODBRIDGE:** hard constraint filters (storage, capacity, safe window including travel), ranked offers using the app's real scoring code (`app/matching.py`), a short first-dibs window per organisation, in-app instant claim, and automatic claim expiry with the next organisation offered.
- Every simulated week uses the same organisations and listings for every process, so rows are fairly compared.

## Headline ({RUNS} simulated weeks, median with 5th to 95th percentile)
{TABLE_HEADER}
{table_row(LABELS['baseline'], today)}
{table_row('FOODBRIDGE (first dibs ' + format(params.offer_window_min, '.0f') + ' min)', foodbridge)}
{table_row('FOODBRIDGE, broadcast to eligible only (no first dibs)', broadcast_only)}

## What the numbers say
""" + "\n".join(f"- {x}" for x in findings) + f"""

## Which part matters (drop one part at a time from FOODBRIDGE)
{TABLE_HEADER}
""" + "\n".join(table_row(LABELS[n], scenarios[n]) for n in scenarios) + """

## First-dibs window sweep
""" + TABLE_HEADER + "\n" + "\n".join(table_row(n, s) for n, s in sweep) + """

## Sensitivity (one assumption at a time; share delivered, minutes to claim, wasted trips per 100)
| Setting | Delivered, today | Delivered, FOODBRIDGE | Minutes, today | Minutes, FOODBRIDGE | Wasted, today | Wasted, FOODBRIDGE |
|---|---|---|---|---|---|---|
""" + "\n".join(sens_rows) + f"""

## A world friendlier to today's process
Misfit replies 5% (instead of {as_percent(params.p_blind_yes)}), confirmation 1 minute (instead of {params.confirm_delay_min:.0f}), no-shows 5% (instead of {as_percent(params.p_noshow_mean)}).
{TABLE_HEADER}
{table_row(LABELS['baseline'], friendly_today)}
{table_row('FOODBRIDGE (first dibs ' + format(params.offer_window_min, '.0f') + ' min)', friendly_foodbridge)}

## Assumptions used for the headline
| Assumption | Value |
|---|---|
""" + "\n".join(f"| {k} | {v} |" for k, v in assumptions.items()) + """

## What this model does not show
- Real response times, real no-show rates and real donor behaviour (all assumed). The pilot must measure them; the first-dibs window should then be set from measured response times.
- Declared storage and capacity are assumed accurate.
- Donor effort, volunteers and vehicles, app adoption, network problems and the faster listing from Speak-to-List are not modelled.
- Double-dispatch in today's process (two organisations sent for the same food) is not modelled; leaving it out favours today's process.
- Food safety is not modelled; FOODBRIDGE never certifies food.
- Fairness is measured only as the largest organisation's share of delivered food.
""" 
# Save the report to disk.
open("reports/SIMULATION_REPORT.md", "w", encoding="utf-8").write(report_markdown)

# Charts need matplotlib; if it is not installed we skip them (see the except at the bottom).
try:
    import matplotlib
    # "Agg" means draw to files, not to a window.
    matplotlib.use("Agg")
    # pyplot = the drawing tools.
    import matplotlib.pyplot as plt
    # Labels for the three bars in the headline chart.
    names = ["Today", f"FOODBRIDGE\n(first dibs {params.offer_window_min:.0f} min)", "FOODBRIDGE,\nbroadcast to\neligible only"]
    # The results behind the three bars.
    sets = [today, foodbridge, broadcast_only]
    # Headline chart: three small charts side by side.
    figure, axes = plt.subplots(1, 3, figsize=(11, 3.8))
    # One small chart per metric (completion, minutes, wasted trips); scale turns 0-1 into 0-100 for percentages.
    for axis, key, title, scale in zip(axes, ["completion", "median_time", "wasted_per_100"],
                                     ["Share of listed food delivered (%)", "Minutes to a confirmed claim", "Wasted trips per 100 listings"], [100, 1, 1]):
        # The bar heights.
        medians = [s[key]["median"] * scale for s in sets]
        # How far each whisker goes down (to the 5th percentile)...
        low_errors = [m - s[key]["p5"] * scale for m, s in zip(medians, sets)]
        # ...and up (to the 95th percentile).
        high_errors = [s[key]["p95"] * scale - m for m, s in zip(medians, sets)]
        # Draw the bars with whiskers.
        axis.bar(names, medians, yerr=[low_errors, high_errors], capsize=4, color=["#9AA5B1", "#0B6E4F", "#E0A100"])
        # Title and tidy label size for this small chart.
        axis.set_title(title, fontsize=10)
        axis.tick_params(axis="x", labelsize=8)
    # Big title across the top, reminding everyone it is a simulation.
    figure.suptitle("SIMULATED under stated assumptions (bars: median; whiskers: 5th to 95th percentile)", fontsize=10, color="#8A5300")
    # Tidy the layout, save the picture, close it.
    figure.tight_layout()
    figure.savefig("reports/sim_headline.png", dpi=150)
    plt.close(figure)

    # Second chart: how the first-dibs window length changes things (two charts side by side).
    figure, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    # The x-axis labels.
    labels = ["broadcast"] + [str(w) for w in WINDOWS]
    # Left chart: minutes to a claim for each window length.
    axes[0].plot(labels, [s["median_time"]["median"] for _, s in sweep], marker="o", color="#0B6E4F")
    # A dashed line showing today's process for comparison, plus a legend.
    axes[0].axhline(today["median_time"]["median"], ls="--", color="#9AA5B1", label="today's process")
    axes[0].legend(fontsize=8)
    # Title and axis label for the left chart.
    axes[0].set_title("Minutes to a confirmed claim", fontsize=10)
    axes[0].set_xlabel("first-dibs window (minutes)", fontsize=9)
    # Right chart: share of food delivered for each window length.
    axes[1].plot(labels, [100 * s["completion"]["median"] for _, s in sweep], marker="o", color="#0B6E4F")
    # A dashed line for today's process again.
    axes[1].axhline(100 * today["completion"]["median"], ls="--", color="#9AA5B1")
    # Title and axis label for the right chart.
    axes[1].set_title("Share of listed food delivered (%)", fontsize=10)
    axes[1].set_xlabel("first-dibs window (minutes)", fontsize=9)
    # Big title for the second chart.
    figure.suptitle("SIMULATED: longer exclusive windows slow claims down", fontsize=10, color="#8A5300")
    # Tidy the layout, save the picture, close it.
    figure.tight_layout()
    figure.savefig("reports/sim_window_sweep.png", dpi=150)
    plt.close(figure)
    # Let the person know the pictures were made.
    print("charts written")
# matplotlib is missing, so say we skipped the charts.
except ImportError:
    print("matplotlib not installed: charts skipped")
# Always tell the person the report was written.
print("wrote reports/SIMULATION_REPORT.md")

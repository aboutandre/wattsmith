"""Seasonal regression guard for the arbitrage planner (hel-130).

A reduced autumn-to-spring sweep from seasonal_sim.py: the planner sees only
forecasts (learned mean load, pessimistic Solcast, prices published at 13:00) and
is billed on actuals. Guards two things on every change:

  - the algorithm: with a perfect forecast it stays on the clairvoyant optimum;
  - production: with realistic forecast error the season stays within a few % of it.

The full sweep and the load-forecast comparison live in seasonal_sim.py:
    python3 tests/seasonal_sim.py 8
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seasonal_sim as ss  # noqa: E402


def test_winter_season_stays_near_optimal():
    eps = ss.season(episodes_per_month=2, days=7)
    opt = sum(ss.dp_optimum(ep) for ep in eps)
    perfect = sum(ss.simulate(ep, oracle=True).cost_ct for ep in eps)
    prod = sum(ss.simulate(ep, "mean", "pessimistic").cost_ct for ep in eps)
    print(f"\n  optimum {opt / 100:.2f} EUR  perfect {perfect / 100:.2f}  production {prod / 100:.2f}")
    assert perfect <= opt * 1.02, f"algorithm regret {(perfect - opt) / opt:.1%} on perfect forecasts"
    assert prod <= opt * 1.04, f"production regret {(prod - opt) / opt:.1%} over the season"


def test_spike_prices_are_mostly_served_from_the_fleet():
    """Energy imported at >= 50 ct while the fleet sits empty, as a share of all the
    house needs at those prices — the 'empty before the spike' failure.

    Dunkelflaute weeks are excluded: when every hour is dear for days there is no
    cheaper window to buy from, and a perfect forecast imports just as much there
    (59 vs 58 kWh in the full sweep). On single-spike weeks the full sweep measures
    7% (realistic forecasts) against 0.7% (perfect forecasts).
    """
    eps = [ep for ep in ss.season(episodes_per_month=2, days=7)
           if "dunkelflaute" not in ep.regimes[:ep.days]]
    exposed = need = 0.0
    for ep in eps:
        exposed += ss.simulate(ep, "mean", "pessimistic").spike_import_wh
        n = ep.days * ss.DAY
        need += sum(max(0.0, ep.load[t] - ep.pv[t]) for t in range(n) if ep.price[t] >= ss.SPIKE_CT)
    print(f"\n  spike-price need {need / 1000:.1f} kWh, imported empty {exposed / 1000:.1f} kWh")
    assert need > 0
    assert exposed / need <= 0.15, f"{exposed / need:.0%} of spike-price load imported with an empty fleet"


if __name__ == "__main__":
    test_winter_season_stays_near_optimal()
    test_spike_prices_are_mostly_served_from_the_fleet()
    print("seasonal tests passed ✓")

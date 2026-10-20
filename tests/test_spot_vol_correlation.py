from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from hedge.config import NY, Manifest
from hedge.models import Contract
from hedge.scenarios import ScenarioEngine


def renamed(manifest, symbol, **update):
    data = manifest.model_dump()
    data["contracts"] = [{**c, "symbol": symbol} for c in data["contracts"]]
    return Manifest.model_validate({**data, **update})


def corr(a, b):
    return float(np.corrcoef(a, b)[0, 1])


def test_index_scenarios_raise_iv_when_spot_falls(manifest, snapshot, quotes, now):
    m = renamed(manifest, "SPY")
    sc = ScenarioEngine(m).build(snapshot, quotes, now)
    spot, iv = sc.changes[:, sc.ids.index(10)], sc.iv_shocks["SPY"]
    assert sc.spot_vol_correlation == {"SPY": -0.7}
    assert "spot-vol corr SPY -0.70" in sc.source
    assert corr(spot, iv) < -0.5
    # Antithetic pairs stay antithetic in spot and in the whole IV shock.
    half = len(iv) // 2
    np.testing.assert_allclose(iv[:half], -iv[half:])
    np.testing.assert_allclose(spot[:half], -spot[half:])
    # The long straddle's vega now gains on down moves: its P&L leans against spot on both horizons.
    for changes in (sc.changes, sc.next_changes):
        straddle = changes[:, sc.ids.index(101)] + changes[:, sc.ids.index(102)]
        assert corr(straddle, changes[:, sc.ids.index(10)]) < 0
    again = ScenarioEngine(m).build(snapshot, quotes, now)
    assert again.identifier == sc.identifier
    np.testing.assert_array_equal(again.iv_shocks["SPY"], iv)
    np.testing.assert_array_equal(again.next_changes, sc.next_changes)


def test_single_name_default_is_milder(manifest, snapshot, quotes, now):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    assert sc.spot_vol_correlation == {"BA": -0.4}
    assert abs(corr(sc.changes[:, sc.ids.index(10)], sc.iv_shocks["BA"]) + 0.4) < 0.1
    spots, ivs = [], []
    for seed in range(1, 21):
        m = manifest.model_copy(update={"policy": manifest.policy.model_copy(update={"seed": seed})})
        s = ScenarioEngine(m).build(snapshot, quotes, now, (10,))
        spots.append(s.changes[:, 0])
        ivs.append(s.iv_shocks["BA"])
    assert abs(corr(np.concatenate(spots), np.concatenate(ivs)) + 0.4) < 0.06


def daily_history(path, now):
    days = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=70)
    rng = np.random.default_rng(5)
    market = rng.normal(0, 0.01, len(days))
    records = []
    for symbol, beta in (("BA", 1.3), ("SPY", 1.0)):
        prices = 100 * np.exp(np.cumsum(beta * market + rng.normal(0, 0.004, len(days))))
        records.extend(
            {"date": str(d.date()), "symbol": symbol, "close": p} for d, p in zip(days, prices, strict=True)
        )
    pd.DataFrame(records).to_csv(path, index=False)


def test_daily_covariance_path_uses_configured_link_for_every_symbol(
    manifest, snapshot, quotes, now, tmp_path
):
    """The old IV shock reused the Cholesky normal, so BA (first alphabetically) had corr +1."""
    path = tmp_path / "daily.csv"
    daily_history(path, now)
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={
            "contracts": (*manifest.contracts, spy),
            "allowlist": (*manifest.allowlist, 200),
            "daily_stock_history": str(path),
        }
    )
    qs = {**quotes, 200: quotes[10].model_copy(update={"con_id": 200, "spot": 450, "iv": 0.18})}
    ids = (10, 101, 102, 200)
    sc = ScenarioEngine(m).build(snapshot, qs, now, ids)
    assert "shrunk covariance" in sc.source and "spot-vol corr BA -0.40 SPY -0.70" in sc.source
    assert sc.spot_vol_correlation == {"BA": -0.4, "SPY": -0.7}
    assert corr(sc.changes[:, ids.index(200)], sc.iv_shocks["SPY"]) < -0.5
    assert corr(sc.changes[:, ids.index(10)], sc.iv_shocks["BA"]) < 0
    flipped = Manifest.model_validate({**m.model_dump(), "spot_vol_correlation": {"ba": 0.3, " spy ": -0.9}})
    other = ScenarioEngine(flipped).build(snapshot, qs, now, ids)
    assert other.spot_vol_correlation == {"BA": 0.3, "SPY": -0.9}
    # 32 antithetic pairs per build: check the level on pooled seeds, the sign per build.
    for config, expected in ((m, {"BA": -0.4, "SPY": -0.7}), (flipped, {"BA": 0.3, "SPY": -0.9})):
        spots, ivs = {s: [] for s in expected}, {s: [] for s in expected}
        for seed in range(1, 21):
            seeded = config.model_copy(update={"policy": config.policy.model_copy(update={"seed": seed})})
            built = ScenarioEngine(seeded).build(snapshot, qs, now, (10, 200))
            for column, symbol in enumerate(("BA", "SPY")):
                spots[symbol].append(built.changes[:, column])
                ivs[symbol].append(built.iv_shocks[symbol])
        for symbol, rho in expected.items():
            assert abs(corr(np.concatenate(spots[symbol]), np.concatenate(ivs[symbol])) - rho) < 0.08
    # Spot paths do not depend on the spot-vol setting; only option IV legs and the identifier move.
    np.testing.assert_array_equal(other.changes[:, [0, 3]], sc.changes[:, [0, 3]])
    assert other.identifier != sc.identifier
    assert not np.array_equal(other.changes[:, 1], sc.changes[:, 1])


def test_matched_clock_history_keeps_observed_iv(manifest, snapshot, quotes, now):
    rows, observed = [], []
    for k, day in enumerate(pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=12)):
        start = pd.Timestamp(day.date()).tz_localize(NY) + timedelta(hours=10)
        spot, iv = 195 + k, 0.25 + 0.002 * k
        move, vol = 0.004 * (-1) ** k, 0.01 * (-1) ** (k // 2)
        observed.append(vol)
        rows += [
            {"timestamp": start, "symbol": "BA", "spot": spot, "iv": iv},
            {
                "timestamp": start + timedelta(hours=6),
                "symbol": "BA",
                "spot": spot * (1 + move),
                "iv": iv + vol,
            },
        ]
    frame = pd.DataFrame(rows)
    frame["timestamp"] = frame.timestamp.dt.tz_convert("UTC")
    sc = ScenarioEngine(manifest, frame).build(snapshot, quotes, now)
    assert sc.source == "historical matched-clock joint spot/IV paths"
    assert sc.spot_vol_correlation == {}
    np.testing.assert_allclose(sc.iv_shocks["BA"], observed)


def test_spot_vol_configuration_bounds_and_defaults(manifest):
    for symbol in ("SPY", "ivv", "VOO", "SPLG", "QQQ", "IWM", "DIA"):
        assert manifest.spot_vol_rho(symbol) == -0.7
    assert manifest.spot_vol_rho("SPCX") == manifest.spot_vol_rho("RKLB") == -0.4
    custom = manifest.model_copy(update={"spot_vol_correlation": {"RKLB": -0.6}})
    assert custom.spot_vol_rho("rklb") == -0.6 and custom.spot_vol_rho("SPY") == -0.7
    data = manifest.model_dump()
    for bad in ({"SPY": -0.96}, {"SPY": 0.51}, {"spy": -0.5, "SPY": -0.6}, {" ": -0.5}):
        with pytest.raises(ValidationError):
            Manifest.model_validate({**data, "spot_vol_correlation": bad})
    assert Manifest.model_validate({**data, "spot_vol_correlation": {"ba": -0.95}}).spot_vol_correlation == {
        "BA": -0.95
    }
    with pytest.raises(ValidationError, match="not in contracts"):  # typo such as SPXC for SPCX
        Manifest.model_validate({**data, "spot_vol_correlation": {"BAA": -0.5}})

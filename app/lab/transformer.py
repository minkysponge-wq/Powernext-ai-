"""Balanced three-phase copper transformer calculations, explicit SI units.

This profile reproduces the supplied 250 kVA Dyn transformer worksheet.
Terminal resistances are line-to-line: delta phase R = 1.5 Rll;
star phase R = 0.5 Rll. No nameplate or limit is inferred for other assets.
"""

from math import isfinite, sqrt

V217_SQRT3 = 1.732


def v217_round(value, places):
    """Printed v2.17 worksheet intermediate precision (positive magnitudes)."""
    return round(value, places)


def number(value):
    value = float(value)
    if not isfinite(value):
        raise ValueError("A finite numeric reading is required.")
    return value


def resistance_at_75(resistance_ohm, temperature_c):
    resistance_ohm, temperature_c = number(resistance_ohm), number(temperature_c)
    if resistance_ohm <= 0 or temperature_c <= -235:
        raise ValueError("Invalid copper resistance or temperature.")
    return resistance_ohm * 310 / (235 + temperature_c)


def load_loss_at_75(measured_w, copper_w, temperature_c):
    measured_w, copper_w, temperature_c = map(number, (measured_w, copper_w, temperature_c))
    if copper_w < 0 or measured_w < copper_w or temperature_c <= -235:
        raise ValueError(
            "Measured load loss must be at least the calculated copper loss; investigate inputs."
        )
    factor = 310 / (235 + temperature_c)
    stray = (measured_w - copper_w) / factor
    return copper_w * factor + stray, stray


def impedance_at_75(z_measured_percent, load_loss_test_w, load_loss_75_w, rated_va, frequency_hz):
    z, pt, p75, va, frequency = map(
        number, (z_measured_percent, load_loss_test_w, load_loss_75_w, rated_va, frequency_hz)
    )
    if va <= 0 or frequency <= 0 or min(z, pt, p75) < 0:
        raise ValueError("Invalid impedance inputs.")
    radicand = z * z - (100 * pt / va) ** 2
    if radicand < 0:
        raise ValueError("Resistive component exceeds measured impedance.")
    x50 = sqrt(radicand) * 50 / frequency
    r75 = 100 * p75 / va
    # The v2.17 worksheet carries reactance and resistance percentage at
    # three decimals into the final impedance magnitude.
    z75 = sqrt(round(x50, 3) ** 2 + round(r75, 3) ** 2)
    return z75, x50, x50 / r75 if r75 else None


def total_loss(no_load_w, full_load_w, fraction):
    no_load_w, full_load_w, fraction = map(number, (no_load_w, full_load_w, fraction))
    if min(no_load_w, full_load_w, fraction) < 0:
        raise ValueError("Losses and load fraction cannot be negative.")
    return no_load_w + fraction * fraction * full_load_w


def calculate_datasheet(values):
    """All six tap/stage rows. Missing keys fail explicitly. Values are caller-reviewed.

    Half-load totals use the independently measured 50% test, not an assumed
    quarter of the 100% result. The generic total_loss function supports scaling.
    """
    n = lambda key: number(values[key])
    avg = lambda prefix, col: sum(n(f"{prefix}.{col}{i}") for i in (1, 2, 3)) / 3
    if str(values["material"]).lower() not in ("cu", "copper") or n("phases") != 3:
        raise ValueError("This worksheet profile requires three-phase copper windings.")
    if (
        n("rated_power") != 250
        or n("rated_hv") != 11000
        or n("rated_lv") != 433
        or n("efficiency_level") != 1
    ):
        raise ValueError(
            "This worksheet profile is scoped to the supplied 250 kVA, 11 kV/433 V EEL-1 asset."
        )
    va = n("rated_power") * 1000
    lv = n("rated_lv")
    rows = []
    for i, (stage, tap) in enumerate(
        zip(("NTBT", "NTAT", "HTBT", "HTAT", "LTBT", "LTAT"), (1, 1, 1.05, 1.05, 0.9, 0.9))
    ):
        prefix, period = f"load_measurement.{i}", i % 2
        if values[prefix + ".stage"] != stage:
            raise ValueError("Unexpected tap/stage ordering.")
        temp = n(f"lv_resistance.{period}.temperature")
        hv = n("rated_hv") * tap
        ih, il = va / (V217_SQRT3 * hv), va / (V217_SQRT3 * lv)
        rh, rl = avg(f"hv_resistance.{i}", "R"), avg(f"lv_resistance.{period}", "R") / 1000
        current = avg(prefix, "I")
        if current <= 0:
            raise ValueError("Test current must be positive.")
        test_loss = v217_round(
            sum(n(prefix + f".P{j}") for j in (1, 2, 3)) * (ih / current) ** 2, 2
        )
        copper = v217_round(1.5 * (ih * ih * rh + il * il * rl), 2)
        factor = v217_round(310 / (235 + temp), 4)
        stray75 = (test_loss - copper) / factor
        load75 = v217_round(copper * factor + stray75, 3)
        ztest = 100 * avg(prefix, "V") * V217_SQRT3 / hv * ih / current
        z75, x50, xr75 = impedance_at_75(ztest, test_loss, load75, va, n(prefix + ".f"))
        no_load = n(f"no_load.{period}.P")
        row = {
            "stage": stage,
            "Rhv": rh * 1.5 * factor,
            "Rlv": rl / 2 * factor * 1000,
            "load_loss_100": load75,
            "stray_loss": stray75,
            "stray_share_percent": 100 * stray75 / load75 if load75 else None,
            "Z75": z75,
            "X50": x50,
            "XR75": xr75,
            "total_loss_100": total_loss(no_load, load75, 1),
            "limit_100": n("guarantee_100"),
        }
        row["verdict_100"] = (
            "Within limit" if row["total_loss_100"] <= row["limit_100"] else "Exceeds limit"
        )
        if i < 2:
            half_current = n(f"half_load.{period}.I")
            if half_current <= 0:
                raise ValueError("Half-load current must be positive.")
            half_measured = v217_round(
                n(f"half_load.{period}.P") * (ih * 0.5 / half_current) ** 2, 2
            )
            half_copper = v217_round(copper * 0.25, 2)
            half75 = v217_round(half_copper * factor + (half_measured - half_copper) / factor, 3)
            row.update(
                load_loss_50=half75, total_loss_50=no_load + half75, limit_50=n("guarantee_50")
            )
            row["verdict_50"] = (
                "Within limit" if row["total_loss_50"] <= row["limit_50"] else "Exceeds limit"
            )
        rows.append(row)
    return rows

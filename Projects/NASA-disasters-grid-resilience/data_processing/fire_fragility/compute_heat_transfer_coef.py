import math

import numpy as np
import rasterio


def read_wind(path):
    with rasterio.open(path) as src:
        arr = src.read().astype("float64")
        nodata = src.nodata
        crs = src.crs
        transform = src.transform
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    arr = np.where(np.isfinite(arr), arr, np.nan)
    return arr, crs, transform


def area_mean_vector_components(wind_path):

    wind, crs_wind, transform_wind = read_wind(wind_path)
   
    mw = np.nanmean(wind, axis=(1,2))
    mean_vector_magnitude = float(np.sqrt(mw[0]*2 + mw[1]**2 + mw[2]))

    return mw, mean_vector_magnitude




def h_empirical_air_velocity(wind_m_s):
    """
    Empirical air-side convective heat transfer coefficient:
        h = 1.16 * (10.45 - v + 10 * sqrt(v))
    Valid approximately for air speeds 2-20 m/s.
    Returns h in W/m^2/K
    Source style: Engineering Toolbox empirical relation.
    """
    if wind_m_s < 0:
        raise ValueError("wind_m_s must be non-negative.")
    return 1.16 * (10.45 - wind_m_s + 10.0 * math.sqrt(wind_m_s))


def h_forced_flat_plate(wind_m_s, L_m, k_air, nu_air, Pr):
    """
    Average forced convection over a flat plate.

    Steps:
      Re = U * L / nu
      If laminar (Re < 5e5):
          Nu = 0.664 * Re^0.5 * Pr^(1/3)
      Else turbulent:
          Nu = (0.037 * Re^0.8 - 871) * Pr^(1/3)
      h = Nu * k / L

    Returns:
      h, Re, Nu, regime
    """
    if wind_m_s <= 0:
        raise ValueError("wind_m_s must be > 0.")
    if L_m <= 0:
        raise ValueError("L_m must be > 0.")
    if k_air <= 0 or nu_air <= 0 or Pr <= 0:
        raise ValueError("Air properties must be positive.")

    Re = wind_m_s * L_m / nu_air

    if Re < 5e5:
        Nu = 0.664 * (Re ** 0.5) * (Pr ** (1.0 / 3.0))
        regime = "laminar"
    else:
        Nu = (0.037 * (Re ** 0.8) - 871.0) * (Pr ** (1.0 / 3.0))
        regime = "turbulent/mixed"

    h = Nu * k_air / L_m
    return h, Re, Nu, regime


def estimate_air_properties_simple(T_film_C):
    """
    Very rough default air properties near ambient-to-hot conditions.
    Replace with better values if you have them.
    Returns:
      k_air [W/m/K], nu_air [m^2/s], Pr [-]
    """
    # Simple placeholders for screening-level calculations.
    # User should replace with property table / Cantera / CoolProp values.
    if T_film_C < 100:
        return 0.026, 1.5e-5, 0.71
    elif T_film_C < 300:
        return 0.035, 2.8e-5, 0.70
    elif T_film_C < 600:
        return 0.050, 6.0e-5, 0.69
    else:
        return 0.075, 1.2e-4, 0.68


if __name__ == "__main__":

    wind_tif = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/input-deck/ws.tif"
    mw, mean_net = area_mean_vector_components(wind_tif)
 
    # Example historic wildfire reconstruction inputs
    T_g_C = 800.0      # hot gas temperature near target
    T_s_C = 100.0       # target surface temperature
    wind_m_s = mean_net     # relative wind speed at target
    L_m = 10          # characteristic length of target surface

    # Optional measured or reconstructed convective heat flux
    q_conv_W_m2 = 25000.0


    print("\n=== Method 2a: Empirical wind-based air correlation ===")
    try:
        h_emp = h_empirical_air_velocity(wind_m_s)
        print(f"h_empirical = {h_emp:.2f} W/m^2/K")
    except ValueError as e:
        print(f"Could not compute h_empirical: {e}")

    print("\n=== Method 2b: Flat-plate forced convection correlation ===")
    T_film_C = 0.5 * (T_g_C + T_s_C)
    k_air, nu_air, Pr = estimate_air_properties_simple(T_film_C)

    try:
        h_plate, Re, Nu, regime = h_forced_flat_plate(
            wind_m_s=wind_m_s,
            L_m=L_m,
            k_air=k_air,
            nu_air=nu_air,
            Pr=Pr
        )
        print(f"T_film = {T_film_C:.1f} C")
        print(f"k_air   = {k_air:.5f} W/m/K")
        print(f"nu_air  = {nu_air:.6e} m^2/s")
        print(f"Pr      = {Pr:.3f}")
        print(f"Re      = {Re:.3e}")
        print(f"Nu      = {Nu:.3f}")
        print(f"Regime  = {regime}")
        print(f"h_plate = {h_plate:.2f} W/m^2/K")
    except ValueError as e:
        print(f"Could not compute h_plate: {e}")

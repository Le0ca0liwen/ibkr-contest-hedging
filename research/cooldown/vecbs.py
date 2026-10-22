"""Vectorized twin of QuantLib's BjerksundStensland/AnalyticEuropean engines for the no-dividend case."""
import numpy as np
from scipy.special import ndtr


def _black_call(S, K, df_r, df_q, var):
    F = S * df_q / df_r
    sd = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = np.log(F / K) / sd + 0.5 * sd
        d2 = d1 - sd
        val = df_r * (F * ndtr(d1) - K * ndtr(d2))
    return np.where(sd > 0, val, df_r * np.maximum(F - K, 0))


def _phi(S, gamma, H, I, rT, bT, var):
    """QuantLib 1.43 phi() (no S**gamma factor)."""
    lam = -rT + gamma * bT + 0.5 * gamma * (gamma - 1.0) * var
    sd = np.sqrt(var)
    d = -(np.log(S / H) + (bT + (gamma - 0.5) * var)) / sd
    kappa = 2.0 * bT / var + (2.0 * gamma - 1.0)
    return np.exp(lam) * (ndtr(d) - np.power(I / S, kappa) * ndtr(d - 2.0 * np.log(I / S) / sd))


def _call93(S, X, rfD, dD, var):
    """QuantLib 1.43 americanCallApproximation() value, same algebraic form."""
    bT = np.log(dD / rfD)
    rT = np.log(1.0 / rfD)
    beta = (0.5 - bT / var) + np.sqrt((bT / var - 0.5) ** 2 + 2.0 * rT / var)
    Binf = beta / (beta - 1.0) * X
    B0 = np.where(bT == rT, X, np.maximum(X, rT / (rT - bT) * X))
    ht = -(bT + 2.0 * np.sqrt(var)) * B0 / (Binf - B0)
    I = B0 + (Binf - B0) * (1 - np.exp(ht))
    fwd = S * dD / rfD
    q = np.log(I / fwd) / np.sqrt(var)
    val = ((I - X) * np.power(S / I, beta) * (1 - _phi(S, beta, I, I, rT, bT, var))
           + S * _phi(S, 1.0, I, I, rT, bT, var) - S * _phi(S, 1.0, X, I, rT, bT, var)
           - X * _phi(S, 0.0, I, I, rT, bT, var) + X * _phi(S, 0.0, X, I, rT, bT, var))
    euro = _black_call(S, X, rfD, dD, var)
    value = np.where(S >= I, np.maximum(0.0, S - X), np.where(q > 12.5, euro, val))
    return np.where(value < euro, euro, value)  # QL: "check if European engine gives higher NPV"


def american(S, K, is_call, r, q, sigma, T, european=False, variant=93):
    """Vector price; r, q continuous rates, T years (QL: discount exp(-r T), variance sigma^2 T)."""
    S, K, r, q, sigma, T = np.broadcast_arrays(*(np.asarray(x, dtype=float) for x in (S, K, r, q, sigma, T)))
    df_r, df_q, var = np.exp(-r * T), np.exp(-q * T), sigma * sigma * T
    if european:
        call = _black_call(S, K, df_r, df_q, var)
        if is_call:
            return call
        F = S * df_q / df_r
        return call - df_r * (F - K)  # put-call parity: Black put
    s, k, dr, dq = (S, K, df_r, df_q) if is_call else (K, S, df_q, df_r)
    black = _black_call(s, k, dr, dq, var)
    with np.errstate(all="ignore"):
        approx = _call93(s, k, dr, dq, var)
    use_black = (dq >= 1.0) & (dq >= dr)
    value = np.where(use_black, black, approx)
    eps = np.finfo(float).eps
    return np.where(value < (s - k) * (1 + 10 * eps), np.maximum(0.0, s - k), value)

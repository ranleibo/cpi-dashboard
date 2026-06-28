"""
update_data.py — GitHub Actions script
=======================================
Fetches CPI data from official sources and writes JSON files to data/ directory.
Runs daily via .github/workflows/update_data.yml

Sources:
  IL  — OECD (ISR), rebased to CBS anchor Nov-2024 = 101.19
  CA  — Statistics Canada WDS API (2002=100 → rebased to 2015=100); OECD fallback
  PL  — Eurostat HICP (2015=100)
  BE  — Eurostat HICP (2015=100)
"""

import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime

# ── API endpoints ─────────────────────────────────────────────────────────────
OECD_NEW_URL = (
    'https://sdmx.oecd.org/public/rest/data/'
    'OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/'
    '{country}.M.CPI.IX.._T.N.?startPeriod=2009-01&format=jsondata'
)

STATSCAN_URL = (
    'https://www150.statcan.gc.ca/t1/tbl1/en/dtbl!18100004/v41690973/200/json'
)

EUROSTAT_URL = (
    'https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/'
    'prc_hicp_midx?format=JSON&unit=I15&coicop=CP00&geo={geo}'
    '&sinceTimePeriod=2009-01&lang=en'
)

CBS_NOV2024_ANCHOR = 101.19   # CBS official November 2024 index (2024=100 base)

# ── HTTP helper ───────────────────────────────────────────────────────────────
def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 CPIDashboard/1.0',
        'Accept':     'application/json',
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode('utf-8'))
    except ssl.SSLError:
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode('utf-8'))


# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_oecd_cpi(data):
    """SDMX-JSON from sdmx.oecd.org → [["dd.mm.yyyy", idx], ...]  newest first."""
    d        = data.get('data', data)
    dims     = d['structure']['dimensions']['observation']
    time_dim = next(dd for dd in dims if dd['id'] == 'TIME_PERIOD')
    time_vals= [v['id'] for v in time_dim['values']]
    t_pos    = dims.index(time_dim)

    monthly = []
    for key, val_arr in d['dataSets'][0]['observations'].items():
        t_idx  = int(key.split(':')[t_pos])
        period = time_vals[t_idx]
        val    = val_arr[0]
        if val is None:
            continue
        yr, mo = period.split('-')
        monthly.append([f'01.{mo}.{yr}', round(float(val), 4)])

    monthly.sort(key=lambda x: (x[0][6:], x[0][3:5]), reverse=True)
    return monthly


def parse_eurostat(data):
    """Eurostat SDMX-JSON → [["dd.mm.yyyy", idx], ...]  newest first."""
    time_cat   = data['dimension']['time']['category']
    time_index = time_cat['index']
    values     = data['value']

    monthly = []
    for period, i in time_index.items():
        val = values.get(str(i))
        if val is None:
            continue
        yr, mo = period.split('-')
        monthly.append([f'01.{mo}.{yr}', round(float(val), 4)])

    monthly.sort(key=lambda x: (x[0][6:], x[0][3:5]), reverse=True)
    return monthly


def parse_statscan(data):
    """Statistics Canada WDS API → [["dd.mm.yyyy", idx], ...]  newest first."""   
  if isinstance(obj, list):
        obj = obj[0] if obj else {}
    obj    = data.get('object', data)

    points = obj.get('vectorDataPoint', [])

    monthly = []
    for pt in points:
        ref = str(pt.get('refPer', ''))
        val = pt.get('value')
        if not ref or val is None or str(val).strip() == '':
            continue
        if 'T' in ref:
            ref = ref[:7]
        parts = ref.split('-')
        if len(parts) < 2:
            continue
        yr, mo = parts[0], parts[1].zfill(2)
        try:
            monthly.append([f'01.{mo}.{yr}', round(float(val), 4)])
        except (ValueError, TypeError):
            pass

    monthly.sort(key=lambda x: (x[0][6:], x[0][3:5]), reverse=True)
    return monthly


# ── Rebase helpers ────────────────────────────────────────────────────────────
def rebase_statscan_to_2015(monthly):
    """Stats Canada 2002=100 → 2015=100 by dividing by Jan-2015 value."""
    ref = next((v for d, v in monthly if d == '01.01.2015'), None)
    if not ref:
        # Try any 2015 month as reference
        ref = next((v for d, v in monthly if d.endswith('.2015')), None)
    if not ref:
        return monthly
    return [[d, round(v / ref * 100, 4)] for d, v in monthly]


def rebase_il_oecd_to_cbs(monthly):
    """OECD Israel (2015=100) → CBS base (Nov-2024 = 101.19)."""
    nov2024 = next((v for d, v in monthly if d == '01.11.2024'), None)
    if not nov2024:
        return monthly
    factor = CBS_NOV2024_ANCHOR / nov2024
    return [[d, round(v * factor, 4)] for d, v in monthly]


# ── Build annual / quarterly from monthly ─────────────────────────────────────
def monthly_to_annual(monthly):
    """One entry per year — latest available month for that year."""
    year_data = {}
    for date_str, idx in monthly:
        parts = date_str.split('.')
        m, y  = int(parts[1]), int(parts[2])
        if y not in year_data or m > year_data[y][0]:
            year_data[y] = (m, date_str, float(idx))
    return [
        {'yr': y, 'date': ds, 'idx': round(idx, 4)}
        for y, (_, ds, idx) in sorted(year_data.items(), reverse=True)
    ]


def monthly_to_quarterly(monthly):
    """One entry per quarter — last month of that quarter (Mar / Jun / Sep / Dec)."""
    q_data = {}
    for date_str, idx in monthly:
        parts = date_str.split('.')
        m, y  = int(parts[1]), int(parts[2])
        q     = (m - 1) // 3 + 1
        lbl   = f'Q{q}-{y}'
        if lbl not in q_data or m > int(q_data[lbl][0].split('.')[1]):
            q_data[lbl] = (date_str, float(idx))
    return [
        {'q': lbl, 'date': ds, 'idx': round(idx, 4)}
        for lbl, (ds, idx) in sorted(q_data.items(), reverse=True)
    ]


def enrich_quarterly(quarters, annual):
    """Add prev_q / same_py / eoy to each quarterly row."""
    q_idx  = {r['q']: r['idx'] for r in quarters}
    yr_end = {}

    # Build year-end map: December of each year is the eoy for next year's quarters
    # Prefer actual December monthly data
    dec_by_year = {}
    for r in quarters:
        if r['q'].startswith('Q4-'):
            yr = int(r['q'].split('-')[1])
            dec_by_year[yr] = r['idx']
    for r in annual:
        if r['yr'] not in dec_by_year:
            dec_by_year[r['yr']] = r['idx']

    result = []
    for r in quarters:
        qp, yr_s = r['q'].split('-')
        qn, yr   = int(qp[1]), int(yr_s)
        prev_lbl   = f'Q{qn-1}-{yr}' if qn > 1 else f'Q4-{yr-1}'
        samepy_lbl = f'{qp}-{yr-1}'
        eoy_val    = dec_by_year.get(yr - 1, r['idx'])
        result.append({
            **r,
            'prev_q':  round(q_idx.get(prev_lbl,   r['idx']), 4),
            'same_py': round(q_idx.get(samepy_lbl, r['idx']), 4),
            'eoy':     round(eoy_val, 4),
        })
    return result


# ── Per-country fetch functions ───────────────────────────────────────────────
def fetch_il():
    """Israel: OECD ISR API, rebased to CBS anchor."""
    raw     = fetch_json(OECD_NEW_URL.format(country='ISR'))
    monthly = parse_oecd_cpi(raw)
    if len(monthly) < 12:
        raise ValueError(f'OECD ISR: only {len(monthly)} rows')
    monthly = rebase_il_oecd_to_cbs(monthly)
    annual  = monthly_to_annual(monthly)
    qtr     = enrich_quarterly(monthly_to_quarterly(monthly), annual)
    print(f'[IL] OECD ISR: {len(monthly)} months, {len(annual)} years')
    return {
        'source':    'OECD (ISR) rebased to CBS Nov-2024=101.19',
        'country':   'IL',
        'updated':   datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'annual':    annual,
        'quarterly': qtr,
        'monthly':   monthly,
    }


def fetch_ca():
    """Canada: Statistics Canada → OECD fallback."""
    # 1. Statistics Canada (official, most current)
    try:
        raw     = fetch_json(STATSCAN_URL, timeout=20)
        monthly = parse_statscan(raw)
        if len(monthly) < 12:
            raise ValueError('too few rows')
        monthly = rebase_statscan_to_2015(monthly)
        src     = 'Statistics Canada (2015=100)'
    except Exception as e1:
        print(f'[CA] StatCan failed: {e1} — trying OECD CAN')
        raw     = fetch_json(OECD_NEW_URL.format(country='CAN'))
        monthly = parse_oecd_cpi(raw)
        if len(monthly) < 12:
            raise ValueError(f'OECD CAN: only {len(monthly)} rows')
        src = 'OECD (CAN) 2015=100'

    annual  = monthly_to_annual(monthly)
    qtr     = enrich_quarterly(monthly_to_quarterly(monthly), annual)
    print(f'[CA] {src}: {len(monthly)} months')
    return {
        'source':     src,
        'country':    'CA',
        'updated':    datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'bases':      [{'v': 100.0, 'label': '2015=100'}],
        'displayBase': 100.0,
        'annual':     annual,
        'quarterly':  qtr,
        'monthly':    monthly,
    }


def fetch_pl():
    """Poland: Eurostat HICP."""
    raw     = fetch_json(EUROSTAT_URL.format(geo='PL'))
    monthly = parse_eurostat(raw)
    if len(monthly) < 12:
        raise ValueError(f'Eurostat PL: only {len(monthly)} rows')
    annual  = monthly_to_annual(monthly)
    qtr     = enrich_quarterly(monthly_to_quarterly(monthly), annual)
    print(f'[PL] Eurostat HICP: {len(monthly)} months')
    return {
        'source':     'Eurostat HICP (2015=100)',
        'country':    'PL',
        'updated':    datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'bases':      [{'v': 100.0, 'label': '2015=100'}],
        'displayBase': 100.0,
        'annual':     annual,
        'quarterly':  qtr,
        'monthly':    monthly,
    }


def fetch_be():
    """Belgium: Eurostat HICP."""
    raw     = fetch_json(EUROSTAT_URL.format(geo='BE'))
    monthly = parse_eurostat(raw)
    if len(monthly) < 12:
        raise ValueError(f'Eurostat BE: only {len(monthly)} rows')
    annual  = monthly_to_annual(monthly)
    qtr     = enrich_quarterly(monthly_to_quarterly(monthly), annual)
    print(f'[BE] Eurostat HICP: {len(monthly)} months')
    return {
        'source':     'Eurostat HICP (2015=100)',
        'country':    'BE',
        'updated':    datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'bases':      [{'v': 100.0, 'label': '2015=100'}],
        'displayBase': 100.0,
        'annual':     annual,
        'quarterly':  qtr,
        'monthly':    monthly,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
FETCHERS = {
    'IL': fetch_il,
    'CA': fetch_ca,
    'PL': fetch_pl,
    'BE': fetch_be,
}

def main():
    os.makedirs('data', exist_ok=True)
    errors = []

    for code, fn in FETCHERS.items():
        try:
            data = fn()
            path = f'data/{code}.json'
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f'[OK] wrote {path}')
        except Exception as e:
            print(f'[ERROR] {code}: {e}', file=sys.stderr)
            errors.append(f'{code}: {e}')

    if errors:
        print(f'\n{len(errors)} error(s):', file=sys.stderr)
        for err in errors:
            print(f'  - {err}', file=sys.stderr)
        # Exit 0 anyway — partial success is still a commit worth making
    else:
        print('\nAll countries updated successfully.')


if __name__ == '__main__':
    main()

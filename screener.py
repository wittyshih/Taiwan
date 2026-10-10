#!/usr/bin/env python3
"""TWSE + TPEx top-300 gainers; daily/weekly/monthly KD(26,3,3) and volume >=3.2x.

Uses official exchange endpoints. Prefilters by daily volume to avoid fetching
many months of history for stocks that cannot possibly pass.
"""
import datetime as dt
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / 'results.json'
LOG = logging.getLogger('screen')
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
})


def get(url, params=None):
    r = SESSION.get(url, params=params, timeout=12, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise RuntimeError(f'API redirected (HTTP {r.status_code}); location={r.headers.get("Location", "")[:100]}')
    r.raise_for_status()
    try:
        result = r.json()
    except ValueError as exc:
        raise RuntimeError(f'API returned non-JSON HTTP {r.status_code}; content-type={r.headers.get("Content-Type", "")}; body={r.text[:100]!r}') from exc
    if not isinstance(result, dict):
        raise RuntimeError('Unexpected API JSON structure')
    return result


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r'<[^>]+>', '', str(v or '')).replace(',', '').replace('+', '').replace('%', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    p = re.findall(r'\d+', str(v))
    if len(p) < 3:
        return None
    y, m, d = map(int, p[:3])
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def roc_date(day):
    return f'{day.year-1911}/{day.month:02d}/{day.day:02d}'


def listed_quotes(day):
    j = get('https://www.twse.com.tw/exchangeReport/MI_INDEX', {
        'response': 'json', 'date': day.strftime('%Y%m%d'), 'type': 'ALLBUT0999'})
    tables = j.get('tables') or []
    table = next((t for t in tables if '證券代號' in (t.get('fields') or [])), None)
    rows = table.get('data', []) if table else j.get('data9', [])
    if not rows:
        raise RuntimeError('TWSE daily quote data unavailable')
    result = []
    for row in rows:
        if len(row) < 11:
            continue
        code = re.sub(r'<[^>]+>', '', str(row[0])).strip()
        if not re.fullmatch(r'\d{4}', code):
            continue
        close, delta, volume = num(row[8]), num(row[10]), num(row[2])
        if None in (close, delta, volume) or close - delta <= 0:
            continue
        result.append({'code': code, 'name': re.sub(r'<[^>]+>', '', str(row[1])).strip(),
                       'market': 'TWSE', 'gain_pct': round(delta / (close-delta) * 100, 4),
                       'close': close, 'volume': volume})
    if not result:
        raise RuntimeError('TWSE daily quotes contained no usable stocks')
    return result


def otc_quotes(day):
    j = get('https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes', {
        'date': roc_date(day), 'id': '', 'response': 'json'})
    tables = j.get('tables') or []
    table = next((t for t in tables if t.get('data') and len(t['data'][0]) >= 10), None)
    if table is None:
        raise RuntimeError('TPEx daily quote data unavailable')
    result = []
    for row in table['data']:
        code = re.sub(r'<[^>]+>', '', str(row[0])).strip()
        if not re.fullmatch(r'\d{4}', code):
            continue
        close, delta, volume = num(row[2]), num(row[3]), num(row[8])
        if None in (close, delta, volume) or close - delta <= 0:
            continue
        result.append({'code': code, 'name': str(row[1]).strip(), 'market': 'TPEx',
                       'gain_pct': round(delta / (close-delta) * 100, 4),
                       'close': close, 'volume': volume})
    if not result:
        raise RuntimeError('TPEx daily quotes contained no usable stocks')
    return result


def month_iter(end, months=32):
    y, m = end.year, end.month
    for _ in range(months):
        yield dt.date(y, m, 1)
        m -= 1
        if m == 0:
            m, y = 12, y - 1


def history_twse(code, end):
    out = {}
    failures = 0
    for day in month_iter(end):
        try:
            j = get('https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY', {
                'response': 'json', 'date': day.strftime('%Y%m%d'), 'stockNo': code})
            if j.get('stat') != 'OK':
                raise RuntimeError(f'TWSE history status: {j.get("stat")}')
            for row in j.get('data', []):
                if len(row) < 7:
                    continue
                date = parse_date(row[0])
                if date and date <= end:
                    high, low, close, vol = num(row[4]), num(row[5]), num(row[6]), num(row[1])
                    if None not in (high, low, close, vol):
                        out[date] = (high, low, close, vol)
            failures = 0
        except (requests.RequestException, RuntimeError) as exc:
            failures += 1
            LOG.warning('TWSE history %s %s: %s', code, day, exc)
            if failures >= 3:
                raise RuntimeError(f'TWSE history unavailable for {code} (3 consecutive failures)') from exc
        time.sleep(0.25)
    return out


def history_tpex(code, end):
    out = {}
    failures = 0
    for day in month_iter(end):
        try:
            j = get('https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock', {
                'code': code, 'date': roc_date(day), 'response': 'json'})
            tables = j.get('tables') or []
            if not tables:
                raise RuntimeError('TPEx history tables unavailable')
            for table in tables:
                for row in table.get('data', []):
                    if len(row) < 7:
                        continue
                    date = parse_date(row[0])
                    if date and date <= end:
                        vol, high, low, close = num(row[1]), num(row[4]), num(row[5]), num(row[6])
                        if None not in (high, low, close, vol):
                            out[date] = (high, low, close, vol)
            failures = 0
        except (requests.RequestException, RuntimeError) as exc:
            failures += 1
            LOG.warning('TPEx history %s %s: %s', code, day, exc)
            if failures >= 3:
                raise RuntimeError(f'TPEx history unavailable for {code} (3 consecutive failures)') from exc
        time.sleep(0.25)
    return out


def aggregate(items, mode):
    if mode == 'daily':
        return [v for _, v in items]
    periods = {}
    for day, (high, low, close, vol) in items:
        key = (day.isocalendar().year, day.isocalendar().week) if mode == 'weekly' else (day.year, day.month)
        if key not in periods:
            periods[key] = [high, low, close, vol]
        else:
            p = periods[key]
            p[0] = max(p[0], high)
            p[1] = min(p[1], low)
            p[2] = close
            p[3] += vol
    return list(periods.values())


def kd(bars, period=26):
    if len(bars) < period:
        return None
    k = d = 50.0
    for i in range(period-1, len(bars)):
        segment = bars[i-period+1:i+1]
        hi = max(x[0] for x in segment)
        lo = min(x[1] for x in segment)
        rsv = 50.0 if hi == lo else (bars[i][2] - lo) / (hi - lo) * 100
        k = (2*k + rsv) / 3
        d = (2*d + k) / 3
    return {'k': round(k, 3), 'd': round(d, 3)}


def evaluate(q, end):
    history = history_twse(q['code'], end) if q['market'] == 'TWSE' else history_tpex(q['code'], end)
    items = sorted(history.items())
    if len(items) < 2:
        return None, 'insufficient daily data'
    if items[-1][0] != end:
        return None, 'history not up to date'
    prev_vol = items[-2][1][3]
    if prev_vol <= 0:
        return None, 'previous volume zero'
    ratio = items[-1][1][3] / prev_vol
    if ratio < 3.2:
        return None, 'volume ratio below 3.2'
    kds = {mode: kd(aggregate(items, mode)) for mode in ('daily', 'weekly', 'monthly')}
    if any(v is None for v in kds.values()):
        return None, 'insufficient KD periods'
    if not all(v['k'] > v['d'] for v in kds.values()):
        return None, 'K not greater than D'
    return {**q, 'volume_ratio': round(ratio, 3), 'kd': kds}, None


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    latest = None
    for offset in range(12):
        day = today - dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            listed, otc = listed_quotes(day), otc_quotes(day)
            latest = (day, listed, otc)
            break
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            LOG.warning('Quote day %s failed: %s', day, exc)
    if latest is None:
        raise RuntimeError('Unable to obtain current TWSE and TPEx quotes; previous results preserved')
    day, listed, otc = latest
    ranked = sorted(listed + otc, key=lambda x: x['gain_pct'], reverse=True)[:300]
    previous = None
    for offset in range(1, 12):
        prior_day = day - dt.timedelta(days=offset)
        if prior_day.weekday() >= 5:
            continue
        try:
            prev_listed, prev_otc = listed_quotes(prior_day), otc_quotes(prior_day)
            previous = (prior_day, {(x['market'], x['code']): x['volume'] for x in prev_listed + prev_otc})
            break
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            LOG.warning('Previous quote day %s failed: %s', prior_day, exc)
    if previous is None:
        raise RuntimeError('Unable to obtain previous trading day volumes; previous results preserved')
    prev_day, prev_volumes = previous
    candidates = []
    missing = 0
    for q in ranked:
        prev = prev_volumes.get((q['market'], q['code']))
        if prev is None or prev <= 0:
            missing += 1
        elif q['volume'] / prev >= 3.2:
            candidates.append(q)
    if missing > len(ranked) // 5:
        raise RuntimeError(f'Previous volume missing for {missing}/{len(ranked)} stocks; previous results preserved')
    LOG.info('Top %s stocks, %s pass volume >=3.2x; previous day %s', len(ranked), len(candidates), prev_day)
    matches, errors, rejected = [], [], defaultdict(int)
    for idx, q in enumerate(candidates, 1):
        try:
            match, reason = evaluate(q, day)
            if match:
                matches.append(match)
            else:
                rejected[reason] += 1
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            LOG.warning('History failed %s %s: %s', q['code'], q['name'], exc)
            errors.append({'code': q['code'], 'error': str(exc)[:180]})
        LOG.info('Progress %s/%s matches=%s errors=%s', idx, len(candidates), len(matches), len(errors))
        if len(errors) >= 3:
            raise RuntimeError('Multiple history API failures; stopping early and preserving previous results')
    if candidates and not matches and sum(rejected.get(k, 0) for k in (
            'history not up to date', 'insufficient daily data', 'insufficient KD periods')) > len(candidates) // 2:
        raise RuntimeError('Most historical data missing; previous results preserved')
    payload = {
        'date': day.isoformat(),
        'generated_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        'universe': 'TWSE + TPEx', 'rank': 'daily percentage gain, top 300',
        'rules': {'kd_period': 26, 'k_smoothing': 3, 'd_smoothing': 3,
                  'kd': 'K > D on daily, weekly, monthly', 'volume_ratio_min': 3.2},
        'candidate_count': len(ranked), 'volume_prefilter_pass_count': len(candidates),
        'evaluated_count': len(candidates) - len(errors), 'match_count': len(matches),
        'error_count': len(errors), 'rejected_counts': dict(rejected),
        'results': matches, 'errors': errors,
    }
    tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(OUT)
    LOG.info('Wrote %s with %s matches', OUT, len(matches))


if __name__ == '__main__':
    main()

"""Pure parsers for verified Xingtu detail response shapes.

The caller owns endpoint, request parameters and browser identity binding.
Only selected fields leave these functions; errors never include API content.
"""

import math
from decimal import Decimal, InvalidOperation


def _success(payload):
    if not isinstance(payload, dict):
        raise ValueError('Detail response must be an object')
    base = payload.get('base_resp')
    if (not isinstance(base, dict) or type(base.get('status_code')) is not int
            or base['status_code'] != 0):
        raise ValueError('Detail response did not explicitly succeed')


def _identifier(value):
    if (not isinstance(value, str) or not value.strip()
            or value != value.strip() or value in ('0', '-') or value.startswith('xingtu:')):
        raise ValueError('Invalid detail identity')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Invalid detail number')
    try:
        exact = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError('Invalid detail number') from None
    if not exact.is_finite() or exact < 0:
        raise ValueError('Detail number must be finite and nonnegative')
    number = float(exact)
    if not math.isfinite(number) or (exact != 0 and number == 0):
        raise ValueError('Detail number is outside the supported range')
    return number


def verify_base(payload: dict, creator: dict, star_id: str) -> dict:
    """Verify base-info identity and return only genuine, namespaced IDs.

    Output uses star_id for response id and uid for core_user_id, retaining
    unique_id/short_id/sec_uid only when present. Every supplied creator ID
    must agree in its own namespace, with at least one matching creator ID.
    A native creator's exact xingtu:STAR storage key is not a Douyin sec_uid.
    """
    _success(payload)
    expected_star = _identifier(star_id)
    if not isinstance(creator, dict):
        raise ValueError('Creator identity must be an object')
    actual_star = _identifier(payload.get('id'))
    if actual_star != expected_star:
        raise ValueError('Detail star identity mismatch')
    profile = {'star_id': actual_star}
    for source, target in (('core_user_id', 'uid'), ('unique_id', 'unique_id'),
                           ('short_id', 'short_id'), ('sec_uid', 'sec_uid')):
        value = payload.get(source)
        if value is not None:
            profile[target] = _identifier(value)
    matched = False
    for name in ('star_id', 'uid', 'unique_id', 'short_id', 'sec_uid'):
        expected = creator.get(name)
        if expected is None or expected == '':
            continue
        if (name == 'sec_uid' and creator.get('source_platform') == 'xingtu'
                and expected == 'xingtu:' + expected_star
                and creator.get('star_id') == expected_star):
            continue
        if _identifier(expected) != profile.get(name):
            raise ValueError('Detail creator identity mismatch')
        matched = True
    if not matched:
        raise ValueError('No matching creator identity evidence')
    return profile


def parse_fans(payload: dict) -> dict:
    """Return gender/age/province count ratios, or None for absent dimensions.

    Only types 1, 2 and 4 are mapped; type 256 is cities, not provinces.
    Present dimensions must be unambiguous and have a positive finite total.
    """
    _success(payload)
    distributions = payload.get('distributions')
    if not isinstance(distributions, list):
        raise ValueError('Invalid fans distributions')
    names = {1: 'gender', 2: 'age', 4: 'region'}
    result = dict.fromkeys(names.values())
    seen = set()
    for distribution in distributions:
        if not isinstance(distribution, dict):
            raise ValueError('Invalid fans dimension')
        kind = distribution.get('type')
        if type(kind) is not int or kind in seen:
            raise ValueError('Invalid or duplicate fans dimension')
        seen.add(kind)
        if kind not in names:
            continue
        rows = distribution.get('distribution_list')
        if not isinstance(rows, list):
            raise ValueError('Invalid fans dimension rows')
        counts = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid fans count row')
            key = row.get('distribution_key')
            if (not isinstance(key, str) or not key.strip() or key != key.strip()
                    or key in counts):
                raise ValueError('Invalid or duplicate fans distribution key')
            counts[key] = _number(row.get('distribution_value'))
        try:
            total = math.fsum(counts.values())
        except OverflowError:
            raise ValueError('Invalid fans dimension total') from None
        if not math.isfinite(total) or total <= 0:
            raise ValueError('Fans dimension total must be positive and finite')
        result[names[kind]] = {key: count / total for key, count in counts.items()}
    return result


def parse_spread(payload: dict) -> dict:
    """Read play_mid and cpm_21_60 / 100, never substituting expect_vv.

    Caller must bind the spread request to type=1, flow_type=0, range=2.
    The CPM conversion retains precision rather than rounding to UI display.
    """
    _success(payload)
    result = {'median_play_30d': None, 'expected_cpm': None}
    if payload.get('play_mid') is not None:
        result['median_play_30d'] = _number(payload['play_mid'])
    cpm = payload.get('expect_cpm')
    if cpm is not None:
        if not isinstance(cpm, dict):
            raise ValueError('Invalid expected CPM container')
        if cpm.get('cpm_21_60') is not None:
            result['expected_cpm'] = _number(cpm['cpm_21_60']) / 100
    return result


def parse_prices(payload: dict, star_id: str) -> dict:
    """Read enabled task-category-1 prices for this author, types 1 and 2.

    Caller must bind the marketing request to channel=1. At least one of
    enable/is_open must be present; every present flag must be exactly True.
    Custom videos (71), collections (73) and other types are never durations.
    Missing prices remain None; ambiguous active duration quotes are rejected.
    """
    _success(payload)
    expected_star = _identifier(star_id)
    result = {'price_1_20': None, 'price_21_60': None}
    rows = payload.get('price_info')
    if rows is None:
        return result
    if not isinstance(rows, list):
        raise ValueError('Invalid price rows')
    names = {1: 'price_1_20', 2: 'price_21_60'}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid price row')
        kind = row.get('video_type')
        if (row.get('author_id') != expected_star
                or type(row.get('task_category')) is not int or row['task_category'] != 1
                or type(kind) is not int or kind not in names):
            continue
        flags = [row[key] for key in ('enable', 'is_open') if key in row]
        if not flags or any(flag is not True for flag in flags):
            continue
        if kind in seen:
            raise ValueError('Ambiguous active duration price')
        seen.add(kind)
        if row.get('price') is not None:
            result[names[kind]] = _number(row['price'])
    return result

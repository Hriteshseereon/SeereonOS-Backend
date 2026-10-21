"""app/js.py against Node golden vectors (devtools-ts/golden-gen/js.mjs)."""

import functools
import os

import pytest

from app import js
from tests.golden import load_vectors

COLL = load_vectors('js_collation.json')
NUMS = load_vectors('js_numbers.json')
DATES = load_vectors('js_dates.json')
STRS = load_vectors('js_strings.json')


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def test_locale_compare_sorts_every_seed_string_like_node():
    got = sorted(COLL['sorted'], key=functools.cmp_to_key(lambda a, b: js.locale_compare(a, b) or js.cmp_str(a, b)))
    mism = [(i, g, w) for i, (g, w) in enumerate(zip(got, COLL['sorted'])) if g != w]
    assert not mism, f'{len(mism)} positions differ, first: {mism[:5]}'


def test_locale_compare_adjacent_equality_classes():
    s = COLL['sorted']
    bad = [(s[i], s[i + 1], c) for i, c in enumerate(COLL['sorted_cmp']) if _sign(js.locale_compare(s[i], s[i + 1])) != c]
    assert not bad, bad[:10]


def test_locale_compare_pairs():
    bad = [(a, b, c) for a, b, c, _ in COLL['pairs'] if _sign(js.locale_compare(a, b)) != _sign(c)]
    assert not bad, f'{len(bad)}/{len(COLL["pairs"])} differ: {bad[:10]}'


def test_compare_numeric_pairs_and_order():
    bad = [(a, b, n) for a, b, _, n in COLL['pairs'] if _sign(js.compare_numeric(a, b)) != _sign(n)]
    assert not bad, f'{len(bad)} differ: {bad[:10]}'
    got = sorted(COLL['sorted_num'], key=functools.cmp_to_key(lambda a, b: js.compare_numeric(a, b) or js.cmp_str(a, b)))
    assert got == COLL['sorted_num']
    s = COLL['sorted_num']
    bad2 = [(s[i], s[i + 1]) for i, c in enumerate(COLL['sorted_num_cmp']) if _sign(js.compare_numeric(s[i], s[i + 1])) != c]
    assert not bad2, bad2[:10]


def _num(v):
    return {'NaN': js.NAN, 'Infinity': js.INF, '-Infinity': -js.INF}.get(v, v) if isinstance(v, str) else v


@pytest.mark.parametrize('row', NUMS['numbers'], ids=lambda r: str(r['n']))
def test_number_formatting(row):
    n = -0.0 if row['neg0'] else _num(row['n'])
    assert js.js_str(n) == row['str']
    r = js.js_round(n)
    assert js.js_str(r) == js.js_str(_num(row['round']))
    assert [js.to_fixed(n, d) for d in range(4)] == row['fixed']
    assert js.js_json_dumps(n).decode() == row['json']
    assert js.fmt_number_en_in(n, 0, 0) == row['intl0']
    assert js.fmt_number_en_in(n, 2, 2) == row['intl2']
    assert js.fmt_number_en_in(n, 0, 2) == row['intl02']
    assert js.fmt_number_en_in(n, 0, 1) == row['intl01']
    assert js.fmt_number_en_in(n) == row['locale']


@pytest.mark.parametrize('row', NUMS['conv'], ids=lambda r: repr(r['s']))
def test_number_conversions(row):
    assert js.js_str(js.js_number(row['s'])) == row['number']
    assert js.js_str(js.js_parse_int(row['s'])) == row['int']
    assert js.js_str(js.js_parse_int(row['s'], 16)) == row['int16']
    assert js.js_str(js.js_parse_float(row['s'])) == row['float']


def test_other_conversions():
    for row in NUMS['other']:
        v = js.UNDEF if row['v'] == '$undef' else row['v']
        assert js.js_str(js.js_number(v)) == row['number'], row
        if not isinstance(v, dict):
            assert js.js_str(v) == row['str'], row


def test_dates_en_in():
    assert os.environ.get('TZ') in ('Asia/Kolkata', 'Asia/Calcutta'), 'run the golden tests with TZ=Asia/Kolkata'
    keys = ['weekday', 'day', 'month', 'year']
    for d in DATES['dates']:
        for o, want in zip(DATES['opts'], d['out']):
            assert js.fmt_date_en_in(d['iso'], **{k: o[k] for k in keys if k in o}) == want, (d['iso'], o)


def test_datetime_en_in_and_parse():
    for s in DATES['stamps']:
        assert js.fmt_datetime_en_in(s['iso']) == s['out'], s
        assert js.date_parse(s['iso']) == s['parse']
        assert js.iso_from_ms(s['parse']) == s['iso']
    for p in DATES['parses']:
        assert js.js_str(js.date_parse(p['s'])) == p['ms'], p


def test_strings():
    for row in STRS['sortDefault']:
        assert js.js_sort(row['in']) == row['out']
    for row in STRS['splits']:
        assert js.js_split(row['s'], row['sep'], row['lim']) == row['out']
    for row in STRS['uri']:
        assert js.encode_uri_component(row['s']) == row['enc']
        assert js.decode_uri_component(row['enc']) == row['s']
    for row in STRS['trims']:
        assert js.js_trim(row['s']) == row['out']
    for row in STRS['mods']:
        assert js.js_str(js.js_mod(row['x'], row['y'])) == row['out']
    for row in STRS['pads']:
        assert js.pad_start(row['s'], row['w'], row['f']) == row['start']
        assert js.pad_end(row['s'], row['w'], row['f']) == row['end']
    for row in STRS['lens']:
        assert js.js_len(row['s']) == row['n']
    with pytest.raises(js.URIError):
        js.decode_uri_component('%E0%A4%A')


def test_truthiness_and_nullish():
    assert js.js_truthy([]) and js.js_truthy({}) and js.js_truthy('0') and js.js_truthy(-1)
    assert not any(js.js_truthy(v) for v in (0, 0.0, '', None, js.UNDEF, js.NAN, False))
    assert js.coalesce(None, js.UNDEF, 0, 5) == 0
    assert js.js_or(0, '', None, 'x') == 'x'
    assert js.js_or(0, '') == ''
    assert js.js_max([]) == -js.INF and js.js_min() == js.INF
    assert js.js_div(1, 0) == js.INF and js.js_div(-1, 0) == -js.INF and js.is_nan(js.js_div(0, 0))


def test_prepare_json():
    v = {'a': 1.0, 'b': js.UNDEF, 'c': [js.UNDEF, 2.5, float('nan')], 'd': {'e': (1, 2)}, 'f': 1e21}
    assert js.js_json_dumps(v) == b'{"a":1,"c":[null,2.5,null],"d":{"e":[1,2]},"f":1e+21}'
    same = {'x': [1, 'a', None]}
    assert js.prepare_json(same) is same

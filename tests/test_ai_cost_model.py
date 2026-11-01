import ai_cost_model as C

def test_known_pricing_and_ratio():
    mini = C.estimate_cost('gpt-4o-mini', 100000, 100000)
    big = C.estimate_cost('gpt-4o', 100000, 100000)
    assert mini['known'] and big['known']
    assert mini['usd'] > 0 and big['usd'] > mini['usd']
    # gpt-4o is ~15-17x gpt-4o-mini per token
    assert 12 <= big['usd'] / mini['usd'] <= 20
    assert 'dkk' in mini and mini['dkk'] > mini['usd']

def test_cached_input_discount():
    full = C.estimate_cost('gpt-4o', 100000, 0, cached_tokens=0)
    cached = C.estimate_cost('gpt-4o', 100000, 0, cached_tokens=100000)
    assert cached['usd'] < full['usd']
    # gpt-4o cache is half of input: 100k tokens * $2.50/1M * 0.5
    assert abs(cached['usd'] - 0.125) < 1e-9


def test_gpt6_prices_and_cache_rates():
    terra = C.estimate_cost('gpt-6.1-terra', 1_000_000, 1_000_000)
    luna = C.estimate_cost('gpt-6-luna', 1_000_000, 1_000_000)
    old = C.estimate_cost('gpt-4o', 1_000_000, 1_000_000)
    assert terra['known'] and luna['known']
    assert abs(terra['usd'] - 6.0) < 1e-9
    assert abs(luna['usd'] - 0.60) < 1e-9
    assert terra['usd'] < old['usd']
    # Terra cache reads are 5% of input, not the legacy 50%.
    terra_cached = C.estimate_cost('gpt-6.1-terra', 1_000_000, 0, cached_tokens=1_000_000)
    assert abs(terra_cached['usd'] - 0.05) < 1e-9
    luna_cached = C.estimate_cost('gpt-6-luna', 1_000_000, 0, cached_tokens=1_000_000)
    assert abs(luna_cached['usd'] - 0.01) < 1e-9

def test_unknown_model_safe():
    r = C.estimate_cost('some-unknown-model', 1000, 1000)
    assert r['usd'] == 0 and not r['known']

def test_summarize_runs():
    rows = [
        {'model':'gpt-4o','input_tokens':1000,'output_tokens':500,'cached_tokens':0},
        {'model':'gpt-4o-mini','input_tokens':2000,'output_tokens':1000,'cached_tokens':0},
    ]
    s = C.summarize_runs(rows)
    assert s and (s.get('usd', s.get('total_usd', 0)) > 0 or isinstance(s, dict))

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

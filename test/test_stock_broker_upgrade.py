from stock import backtest,fundamental_profiles
def test_cost_model_reduces_round_trip_value():
 assert backtest._entry_cost(100,backtest.DEFAULT_COSTS)>100
 assert backtest._exit_proceeds(100,backtest.DEFAULT_COSTS)<100
def test_out_of_sample_has_holdout():
 n=120; c=[10000+i*10 for i in range(n)]; h=[x+50 for x in c]; l=[x-50 for x in c]; v=[100000.0]*n; d=[f"2024-{1+i//28:02d}-{1+i%28:02d}" for i in range(n)]
 r=backtest.run_out_of_sample_on_series("TEST",c,h,l,v,d,c,h,l,v)
 assert r.train.sample=="train" and r.test.sample=="out_of_sample"
def test_sector_profiles():
 assert fundamental_profiles.get_profile("VCB").benchmark_metric=="pb"
 assert fundamental_profiles.get_profile("SSI").benchmark_metric=="pb"
 assert fundamental_profiles.get_profile("VHM").benchmark_metric=="pb"
 assert fundamental_profiles.get_profile("FPT").benchmark_metric=="pe"


def test_render_runtime_detected_and_denied_by_default():
 env={"RENDER":"true","STOCK_BACKTEST_ALLOW_ON_RENDER":"false"}
 assert backtest.is_render_runtime(env)
 try:
  backtest.assert_backtest_runtime_allowed(env)
 except RuntimeError as error:
  assert "disabled on Render" in str(error)
 else:
  raise AssertionError("Render backtest must be denied")

def test_render_override_is_still_resource_capped():
 env={"RENDER":"true","STOCK_BACKTEST_ALLOW_ON_RENDER":"true"}
 backtest.assert_backtest_runtime_allowed(env)
 days,symbols,concurrency=backtest.backtest_runtime_limits(1260,1700,env)
 assert days==756 and symbols==50 and concurrency==2

def test_non_render_concurrency_is_bounded():
 assert backtest.backtest_runtime_limits(1260,1700,{"STOCK_BACKTEST_CONCURRENCY":"99"})==(1260,1700,8)

from pathlib import Path
def test_stock_package_imports_and_template():
 from stock import analysis,backtest,features,fundamental_profiles,fundamentals,policy,providers,sector,validation
 assert analysis._STOCK_PROMPT_TEMPLATE is not None
 assert (Path(analysis.__file__).parent/"templates"/"stock_analysis_prompt.j2").is_file()
 assert backtest.DEFAULT_BACKTEST_DAYS >= 3*backtest.TRADING_DAYS_PER_YEAR

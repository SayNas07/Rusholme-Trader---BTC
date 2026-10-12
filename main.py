
import sys
sys.stdout.reconfigure(encoding='utf-8')

from xbbg import blp


print("starting...")

try:
    from xbbg import blp
except ImportError:
    print("xbbg not installed - run in terminal:")
    print("  pip install --user blpapi --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/")
    print("  pip install --user xbbg")
    raise SystemExit

tickers = ['IVA US Equity', 'ADCT US Equity', 'ITUB US Equity', 'PRAX US Equity']
print(blp.bdp(tickers, ['PX_LAST', 'CUR_MKT_CAP']))

df = blp.bdp(tickers, ['PX_LAST', 'CUR_MKT_CAP'], backend='pandas')
print(df)
df.to_csv('watchlist.csv')
import sqlite3
import pandas as pd
con = sqlite3.connect("output/simulations/test-local.db")
print(sqlite3.connect("output/simulations/test_local.db").cursor().execute("select sqlite_version()").fetchall()) 

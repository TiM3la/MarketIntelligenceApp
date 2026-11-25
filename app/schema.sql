CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT NOT NULL,kind TEXT DEFAULT 'rss',url TEXT DEFAULT '',category TEXT DEFAULT 'Other',trust INTEGER DEFAULT 1,interval INTEGER DEFAULT 120,enabled INTEGER DEFAULT 1,selector TEXT DEFAULT '',checked REAL,success REAL,error TEXT,next REAL DEFAULT 0,etag TEXT,modified TEXT,cursor INTEGER DEFAULT 0);
CREATE TABLE categories(name TEXT PRIMARY KEY,keywords TEXT);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE watchlist(ticker TEXT PRIMARY KEY,name TEXT,type TEXT,keywords TEXT,category TEXT,assets TEXT);
CREATE TABLE events(id INTEGER PRIMARY KEY,title TEXT,category TEXT,importance TEXT,detected REAL,instruments TEXT,analysis TEXT,ai_next REAL DEFAULT 0,saved INTEGER DEFAULT 0);
CREATE TABLE originals(id INTEGER PRIMARY KEY,source INTEGER REFERENCES sources,event INTEGER REFERENCES events,uid TEXT,title TEXT,raw TEXT,url TEXT,published TEXT,hash TEXT,detected REAL,UNIQUE(source,uid,hash));
CREATE INDEX original_url ON originals(url);
CREATE INDEX original_hash ON originals(hash);
CREATE INDEX original_event ON originals(event);
CREATE TABLE deliveries(kind TEXT,ref INTEGER,sent REAL,next REAL DEFAULT 0,PRIMARY KEY(kind,ref));
CREATE TABLE videos(event INTEGER PRIMARY KEY,url TEXT UNIQUE,status TEXT DEFAULT 'pending',language TEXT,transcript TEXT,report TEXT,next REAL DEFAULT 0,error TEXT);
CREATE TABLE segments(event INTEGER,start REAL,end REAL,text TEXT,PRIMARY KEY(event,start));
CREATE TABLE calendar(id INTEGER PRIMARY KEY,title TEXT,due REAL,source TEXT,category TEXT,result TEXT);


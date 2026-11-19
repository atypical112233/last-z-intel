import os, re, json, sqlite3, urllib.request, urllib.parse, html
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path

BASE=Path(__file__).parent
DB_PATH=os.environ.get("DATABASE_PATH", str(BASE/"lastz.db"))
SOURCE_URL=os.environ.get("SOURCE_URL", "https://last-z.us/rankings?playerServers=66")
app=FastAPI(title="Last Z Intel", version="5.0")

def db():
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS players (
      id INTEGER PRIMARY KEY, uid TEXT UNIQUE, name TEXT, server TEXT, alliance TEXT,
      power INTEGER DEFAULT 0, rank INTEGER, avatar TEXT, source TEXT, updated_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS fetch_log (
      id INTEGER PRIMARY KEY, url TEXT, status TEXT, message TEXT, count INTEGER DEFAULT 0, created_at TEXT
    )""")
    con.commit(); return con

class PlayerIn(BaseModel):
    uid:str|None=None; name:str; server:str="66"; alliance:str|None=None
    power:int=0; rank:int|None=None; avatar:str|None=None; source:str|None=None

@app.get("/",response_class=HTMLResponse)
def home(): return (BASE/"index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health(): return {"ok":True,"version":"5.0","source_url":SOURCE_URL}

@app.get("/api/players")
def players(q:str="",server:str="66",alliance:str="",sort:str="power"):
    con=db()
    order={"power":"power DESC","name":"name COLLATE NOCASE","rank":"rank ASC"}.get(sort,"power DESC")
    sql="SELECT * FROM players WHERE server=?"; args=[server]
    if q: sql+=" AND (name LIKE ? OR uid LIKE ? OR alliance LIKE ?)"; args += [f"%{q}%"]*3
    if alliance: sql+=" AND alliance LIKE ?"; args.append(f"%{alliance}%")
    rows=con.execute(sql+" ORDER BY "+order+" LIMIT 1000",args).fetchall()
    con.close(); return [dict(r) for r in rows]

@app.post("/api/players")
def save_player(p:PlayerIn):
    con=db(); now=datetime.now(timezone.utc).isoformat()
    uid=p.uid or ("local:"+p.server+":"+p.name.lower())
    con.execute("""INSERT INTO players(uid,name,server,alliance,power,rank,avatar,source,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,server=excluded.server,
      alliance=excluded.alliance,power=excluded.power,rank=excluded.rank,avatar=excluded.avatar,
      source=excluded.source,updated_at=excluded.updated_at""",
      (uid,p.name,p.server,p.alliance,p.power,p.rank,p.avatar,p.source,now))
    con.commit(); con.close(); return {"saved":True,"uid":uid}

@app.delete("/api/players/{uid}")
def delete_player(uid:str):
    con=db(); cur=con.execute("DELETE FROM players WHERE uid=?",(uid,)); con.commit(); con.close()
    return {"deleted":cur.rowcount>0}

@app.get("/api/stats")
def stats():
    con=db()
    total=con.execute("SELECT count(*) FROM players").fetchone()[0]
    alliances=con.execute("SELECT count(distinct alliance) FROM players WHERE alliance IS NOT NULL AND alliance!=''").fetchone()[0]
    top=con.execute("SELECT name,power FROM players ORDER BY power DESC LIMIT 1").fetchone()
    last=con.execute("SELECT created_at,status,message,count FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return {"players":total,"alliances":alliances,"top_player":dict(top) if top else None,"last_fetch":dict(last) if last else None}

@app.get("/api/logs")
def logs():
    con=db(); rows=con.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT 20").fetchall(); con.close()
    return [dict(r) for r in rows]

def log(url,status,msg,count=0):
    con=db(); con.execute("INSERT INTO fetch_log(url,status,message,count,created_at) VALUES(?,?,?,?,?)",
      (url,status,msg,count,datetime.now(timezone.utc).isoformat())); con.commit(); con.close()

def parse_source(content, url):
    # Extract likely ranking table rows; deliberately conservative, report counts for source diagnosis.
    parsed=[]
    # JSON attempt: recursively locate objects that resemble player records
    try:
        obj=json.loads(content)
        def walk(x):
            if isinstance(x,dict):
                keys={k.lower() for k in x}
                name=next((x[k] for k in x if k.lower() in ("name","playername","player_name","nickname")),None)
                power=next((x[k] for k in x if k.lower() in ("power","totalpower","playerpower")),0)
                uid=next((x[k] for k in x if k.lower() in ("uid","playerid","player_id","id")),None)
                if name and (power or uid):
                    alliance=next((x[k] for k in x if k.lower() in ("alliance","alliance_name","guild")),None)
                    avatar=next((x[k] for k in x if k.lower() in ("avatar","avatarurl","image","profileimage")),None)
                    parsed.append(dict(uid=str(uid) if uid else None,name=str(name),server="66",alliance=str(alliance) if alliance else None,power=to_num(power),avatar=avatar,source=url))
                for v in x.values(): walk(v)
            elif isinstance(x,list):
                for v in x: walk(v)
        walk(obj)
    except Exception: pass
    if parsed: return parsed
    # HTML table fallback; table headers need player/name, power, alliance/uid fields
    try:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(content,"html.parser")
        for tr in soup.select("tr"):
            cells=tr.find_all(["td","th"])
            textcells=[c.get_text(" ",strip=True) for c in cells]
            if len(textcells)<2: continue
            joined=" ".join(textcells)
            if any(word in joined.lower() for word in ("player","rank","power")): continue
            name=textcells[0].strip()
            if len(name)<2 or len(name)>80: continue
            nums=[to_num(x) for x in textcells[1:]]
            power=max(nums or [0])
            if not power: continue
            img=tr.find("img"); avatar=img.get("src") if img else None
            parsed.append(dict(uid=None,name=name,server="66",alliance=None,power=power,avatar=avatar,source=url))
    except Exception: pass
    return parsed

def to_num(v):
    try:
        s=re.sub(r"[^0-9]","",str(v)); return int(s) if s else 0
    except: return 0

@app.post("/api/fetch")
def fetch():
    url=os.environ.get("SOURCE_URL",SOURCE_URL)
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"LastZIntel/5.0 data-source diagnostic"})
        with urllib.request.urlopen(req,timeout=25) as response:
            content=response.read().decode("utf-8","replace")
            rows=parse_source(content,url)
            con=db()
            for r in rows:
                uid=r.get("uid") or ("auto:"+r["name"].lower().strip())
                con.execute("""INSERT INTO players(uid,name,server,alliance,power,avatar,source,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,server=excluded.server,
                alliance=COALESCE(excluded.alliance,players.alliance),power=excluded.power,
                avatar=COALESCE(excluded.avatar,players.avatar),source=excluded.source,updated_at=excluded.updated_at""",
                (uid,r["name"],"66",r.get("alliance"),r.get("power",0),r.get("avatar"),url,datetime.now(timezone.utc).isoformat()))
            con.commit(); con.close()
            msg=f"Fetched HTTP {response.status}; parsed {len(rows)} candidate player records. Parser may require source-specific tuning."
            log(url,"success",msg,len(rows))
            return {"success":True,"url":url,"count":len(rows),"message":msg,"preview":rows[:10]}
    except Exception as e:
        msg=f"{type(e).__name__}: {str(e)[:500]}"
        log(url,"error",msg)
        raise HTTPException(502,detail={"url":url,"message":msg,"hint":"Check SOURCE_URL or whether the source blocks server requests."})

@app.get("/api/source")
def source(): return {"source_url":os.environ.get("SOURCE_URL",SOURCE_URL)}

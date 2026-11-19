import os, re, json, sqlite3, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
BASE=Path(__file__).parent
DB_PATH=os.environ.get('DATABASE_PATH',str(BASE/'lastz.db'))
SOURCE_TEMPLATE=os.environ.get('SOURCE_TEMPLATE','https://last-z.us/rankings?playerServers={server}')
app=FastAPI(title='Last Z Intel',version='6.0')
def db():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
 c.execute('CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,name TEXT,server TEXT,alliance TEXT,power INTEGER DEFAULT 0,rank INTEGER,avatar TEXT,source TEXT,updated_at TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS fetch_log(id INTEGER PRIMARY KEY,url TEXT,status TEXT,message TEXT,count INTEGER DEFAULT 0,created_at TEXT)'); c.commit(); return c
class PlayerIn(BaseModel): uid:str|None=None; name:str; server:str='66'; alliance:str|None=None; power:int=0; rank:int|None=None; avatar:str|None=None; source:str|None=None
class FetchIn(BaseModel): server:int=66
@app.get('/',response_class=HTMLResponse)
def home(): return (BASE/'index.html').read_text(encoding='utf-8')
@app.get('/api/health')
def health(): return {'ok':True,'version':'6.0','source_template':SOURCE_TEMPLATE}
@app.get('/api/players')
def players(q:str='',server:str='66',alliance:str='',sort:str='power'):
 server=str(int(server)); order={'power':'power DESC','name':'name COLLATE NOCASE','rank':'rank ASC'}.get(sort,'power DESC'); c=db(); sql='SELECT * FROM players WHERE server=?'; args=[server]
 if q: sql+=' AND (name LIKE ? OR uid LIKE ? OR alliance LIKE ?)'; args += [f'%{q}%']*3
 if alliance: sql+=' AND alliance LIKE ?'; args.append(f'%{alliance}%')
 rows=c.execute(sql+' ORDER BY '+order+' LIMIT 1500',args).fetchall(); c.close(); return [dict(r) for r in rows]
@app.post('/api/players')
def save_player(p:PlayerIn):
 c=db(); now=datetime.now(timezone.utc).isoformat(); uid=p.uid or ('local:'+str(p.server)+':'+p.name.lower().strip())
 c.execute('''INSERT INTO players(uid,name,server,alliance,power,rank,avatar,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,server=excluded.server,alliance=excluded.alliance,power=excluded.power,rank=excluded.rank,avatar=excluded.avatar,source=excluded.source,updated_at=excluded.updated_at''',(uid,p.name,str(p.server),p.alliance,p.power,p.rank,p.avatar,p.source,now)); c.commit(); c.close(); return {'saved':True,'uid':uid}
@app.delete('/api/players/{uid}')
def delete_player(uid:str):
 c=db(); cur=c.execute('DELETE FROM players WHERE uid=?',(uid,)); c.commit(); c.close(); return {'deleted':cur.rowcount>0}
@app.get('/api/stats')
def stats(server:str='66'):
 c=db(); total=c.execute('SELECT count(*) FROM players WHERE server=?',(str(server),)).fetchone()[0]; alliance_count=c.execute("SELECT count(distinct alliance) FROM players WHERE server=? AND alliance IS NOT NULL AND alliance!=''",(str(server),)).fetchone()[0]; top=c.execute('SELECT name,power FROM players WHERE server=? ORDER BY power DESC LIMIT 1',(str(server),)).fetchone(); last=c.execute('SELECT created_at,status,message,count FROM fetch_log ORDER BY id DESC LIMIT 1').fetchone(); c.close(); return {'players':total,'alliances':alliance_count,'top_player':dict(top) if top else None,'last_fetch':dict(last) if last else None}
@app.get('/api/logs')
def logs():
 c=db(); rows=c.execute('SELECT * FROM fetch_log ORDER BY id DESC LIMIT 20').fetchall(); c.close(); return [dict(r) for r in rows]
def log(url,status,msg,count=0):
 c=db(); c.execute('INSERT INTO fetch_log(url,status,message,count,created_at) VALUES(?,?,?,?,?)',(url,status,msg,count,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
def power_num(v):
 m=re.search(r'(\d+(?:\.\d+)?)\s*([KMB])?',str(v).replace(',',''),re.I)
 if not m:return 0
 return int(float(m.group(1))*{'K':1e3,'M':1e6,'B':1e9}.get((m.group(2) or '').upper(),1))
def dedupe(rows):
 out=[]; seen=set()
 for r in rows:
  key=r.get('uid') or (r['server']+'|'+r['name'].lower().strip())
  if key not in seen: seen.add(key); out.append(r)
 return out
def parse_players(raw,server,url):
 rows=[]
 # JSON-like sources
 try:
  data=json.loads(raw)
  def walk(obj):
   if isinstance(obj,dict):
    keys={str(k).lower():v for k,v in obj.items()}; nm=next((keys[k] for k in ['name','playername','player_name','nickname'] if keys.get(k)),None); pw=next((keys[k] for k in ['power','totalpower','playerpower'] if keys.get(k)),None); sid=str(keys.get('server',keys.get('state',server)))
    if nm and pw and sid==str(server): rows.append({'uid':str(keys.get('uid',keys.get('playerid',''))) or None,'name':str(nm),'server':str(server),'alliance':str(keys.get('alliance',keys.get('guild',''))) or None,'power':power_num(pw),'rank':keys.get('rank'),'avatar':keys.get('avatar'),'source':url})
    for v in obj.values(): walk(v)
   elif isinstance(obj,list):
    for v in obj: walk(v)
  walk(data)
 except Exception: pass
 if rows:return dedupe(rows)
 try:
  from bs4 import BeautifulSoup
  soup=BeautifulSoup(raw,'html.parser')
  # Table row extraction
  for tr in soup.select('tr'):
   cells=[x.get_text(' ',strip=True) for x in tr.find_all(['td','th'])]; txt=' | '.join(cells)
   if len(cells)>1 and re.search(r'(?:server|state)\s*#?\s*'+re.escape(str(server))+r'\b',txt,re.I):
    p=max([power_num(x) for x in cells]+[0]); im=tr.find('img')
    if p: rows.append({'uid':None,'name':cells[0],'server':str(server),'alliance':None,'power':p,'rank':None,'avatar':im.get('src') if im else None,'source':url})
  # Card extraction. Only retain cards explicitly showing selected state and a power value.
  for node in soup.select('article,li,.player,.rank-item,.ranking-item,.card'):
   txt=node.get_text(' ',strip=True)
   if not re.search(r'(?:server|state)\s*#?\s*'+re.escape(str(server))+r'\b',txt,re.I):continue
   pm=re.search(r'(\d[\d,.]*\s*[KMB])\b',txt,re.I)
   if not pm:continue
   heading=node.select_one('h1,h2,h3,h4,strong,b,a'); nm=heading.get_text(' ',strip=True) if heading else ''
   if not nm: nm=re.split(r'(?:server|state)\s*#?\s*'+re.escape(str(server))+r'\b',txt,flags=re.I)[0].strip(' ·|#')
   im=node.find('img')
   if nm and len(nm)<90: rows.append({'uid':None,'name':nm,'server':str(server),'alliance':None,'power':power_num(pm.group(1)),'rank':None,'avatar':im.get('src') if im else None,'source':url})
 except Exception: pass
 return dedupe(rows)
@app.post('/api/fetch')
def fetch(payload:FetchIn):
 server=str(payload.server)
 if not 1<=int(server)<=1000: raise HTTPException(400,'State/server must be 1–1000')
 url=SOURCE_TEMPLATE.format(server=server)
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; LastZIntel/6.0)','Accept':'text/html,application/json;q=0.9,*/*;q=0.8'})
  with urllib.request.urlopen(req,timeout=30) as response:
   raw=response.read().decode('utf-8','replace'); rows=parse_players(raw,server,url)
   if not rows:
    title=re.search(r'<title[^>]*>(.*?)</title>',raw,re.I|re.S); msg=f'HTTP {response.status} page reached, but no player rows could be verified for State {server}. Parser/source mismatch or unavailable cached records. No import performed.'
    log(url,'empty',msg,0); return {'success':False,'url':url,'count':0,'message':msg,'preview':[],'state':server,'diagnostics':{'html_chars':len(raw),'page_title':title.group(1).strip() if title else 'unknown'}}
   c=db(); now=datetime.now(timezone.utc).isoformat(); n=0
   for r in rows:
    uid=r.get('uid') or ('source:'+server+':'+r['name'].lower().strip())
    c.execute('''INSERT INTO players(uid,name,server,alliance,power,rank,avatar,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,server=excluded.server,alliance=COALESCE(excluded.alliance,players.alliance),power=excluded.power,rank=COALESCE(excluded.rank,players.rank),avatar=COALESCE(excluded.avatar,players.avatar),source=excluded.source,updated_at=excluded.updated_at''',(uid,r['name'],server,r.get('alliance'),r['power'],r.get('rank'),r.get('avatar'),url,now)); n+=1
   c.commit(); c.close(); msg=f'Imported/updated {n} records for State {server}.'; log(url,'success',msg,n); return {'success':True,'url':url,'count':n,'message':msg,'preview':rows[:10],'state':server}
 except Exception as e:
  msg=f'{type(e).__name__}: {str(e)[:400]}'; log(url,'error',msg); raise HTTPException(502,detail={'url':url,'message':msg})
@app.get('/api/source')
def source(): return {'source_template':SOURCE_TEMPLATE}

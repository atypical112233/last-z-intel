import os, re, json, sqlite3, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
BASE=Path(__file__).parent
DB_PATH=os.environ.get('DATABASE_PATH',str(BASE/'lastz.db'))
SOURCE_TEMPLATE=os.environ.get('SOURCE_TEMPLATE','https://last-z.us/rankings?playerServers={server}-{server}')
app=FastAPI(title='Last Z Intel',version='8.0')
def db():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
 c.execute('CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,name TEXT,server TEXT,alliance TEXT,power INTEGER DEFAULT 0,rank INTEGER,avatar TEXT,source TEXT,updated_at TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS fetch_log(id INTEGER PRIMARY KEY,url TEXT,status TEXT,message TEXT,count INTEGER DEFAULT 0,created_at TEXT)'); c.commit(); return c
class PlayerIn(BaseModel): uid:str|None=None; name:str; server:str='66'; alliance:str|None=None; power:int=0; rank:int|None=None; avatar:str|None=None; source:str|None=None
class FetchIn(BaseModel): server:int=66
@app.get('/',response_class=HTMLResponse)
def home(): return (BASE/'index.html').read_text(encoding='utf-8')
@app.get('/api/health')
def health(): return {'ok':True,'version':'8.0','source_template':SOURCE_TEMPLATE}
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
 # First, inspect JSON in the whole response and JSON-bearing script tags.
 payloads=[]
 try: payloads.append(json.loads(raw))
 except Exception: pass
 try:
  from bs4 import BeautifulSoup
  soup=BeautifulSoup(raw,'html.parser')
  for script in soup.select('script[type="application/json"],script#__NEXT_DATA__,script[type="application/ld+json"]'):
   if script.string or script.get_text(strip=True):
    try: payloads.append(json.loads(script.string or script.get_text()))
    except Exception: pass
 except Exception: soup=None
 def walk(obj):
  if isinstance(obj,dict):
   keys={str(k).lower().replace('_',''):v for k,v in obj.items()}
   nm=next((keys[k] for k in ['name','playername','nickname','username','displayname'] if keys.get(k)),None)
   pw=next((keys[k] for k in ['power','totalpower','playerpower','combatpower'] if keys.get(k)),None)
   sid=keys.get('server',keys.get('serverid',keys.get('state',keys.get('stateid',server))))
   if nm and pw and str(sid)==str(server):
    rows.append({'uid':str(keys.get('uid',keys.get('playerid',keys.get('id','')))) or None,'name':str(nm).strip(),'server':str(server),'alliance':str(keys.get('alliance',keys.get('guild',keys.get('allianceName','')))) or None,'power':power_num(pw),'rank':keys.get('rank',keys.get('serverrank')),'avatar':keys.get('avatar',keys.get('image',keys.get('profileimage'))),'source':url})
   for v in obj.values(): walk(v)
  elif isinstance(obj,list):
   for v in obj: walk(v)
 for data in payloads: walk(data)
 if rows: return dedupe(rows)
 if soup is None:
  try:
   from bs4 import BeautifulSoup
   soup=BeautifulSoup(raw,'html.parser')
  except Exception: return []
 # Ranking cards on last-z.us expose phrases like "Global #275 · Server 66 #1".
 server_pat=re.compile(r'\bserver\s*'+re.escape(str(server))+r'\s*#\s*(\d+)',re.I)
 power_pat=re.compile(r'\b(\d[\d,.]*\s*[KMB])\b',re.I)
 global_pat=re.compile(r'\bglobal\s*#\s*(\d+)',re.I)
 filter_pat=re.compile(r'\bfilter\s*#\s*(\d+)',re.I)
 # Consider all DOM block types, then process shortest candidate text first.
 candidates=[]
 for node in soup.find_all(['div','article','li','section','a']):
  txt=node.get_text(' ',strip=True)
  if not txt or len(txt)>1200: continue
  sm=server_pat.search(txt); pm=power_pat.search(txt)
  if sm and pm: candidates.append((len(txt),node,txt,sm,pm))
 candidates.sort(key=lambda x:x[0])
 seen=set()
 for _,node,txt,sm,pm in candidates:
  # Walk up a few ancestors so the player name/avatar can be found if the
  # smallest matching element only contains the rank/power subline.
  card=node
  for _ in range(4):
   if card.parent is None or getattr(card.parent,'name',None) in ('body','html'): break
   parent=card.parent; ptxt=parent.get_text(' ',strip=True)
   if len(ptxt)>1800: break
   if server_pat.search(ptxt) and power_pat.search(ptxt): card=parent
   else: break
  txt=card.get_text(' ',strip=True)
  sm=server_pat.search(txt) or sm; pm=power_pat.search(txt) or pm
  # Prefer semantic heading, link text, image alt/title, then clean text fragments.
  name=''
  for el in card.select('h1,h2,h3,h4,[class*=name],[class*=player-name],[class*=username]'):
   cand=el.get_text(' ',strip=True)
   if cand and len(cand)<=80 and not re.search(r'(server|global|filter|rank|\d+\s*[KMB])',cand,re.I): name=cand; break
  if not name:
   for el in card.select('img[alt],img[title],a[title]'):
    cand=(el.get('alt') or el.get('title') or '').strip()
    if cand and len(cand)<=80 and not re.search(r'(server|global|filter|rank)',cand,re.I): name=cand; break
  if not name:
   # Strip rank metadata and power from visible text; first plausible token is usually the name.
   cleaned=re.sub(r'\bFilter\s*#?\s*\d+\b|\bGlobal\s*#?\s*\d+\b|\bServer\s*'+re.escape(str(server))+r'\s*#?\s*\d+\b',' ',txt,flags=re.I)
   cleaned=re.sub(r'\b\d[\d,.]*\s*[KMB]\b',' ',cleaned,flags=re.I)
   parts=[x.strip(' ·|#:-') for x in re.split(r'[·|\n]+',cleaned) if x.strip()]
   for cand in parts:
    if 1<=len(cand)<=80 and not re.search(r'^(rank|power|server|global|filter|irt)$',cand,re.I): name=cand; break
  if not name or name.lower() in seen: continue
  seen.add(name.lower())
  img=card.find('img'); avatar=(img.get('src') or img.get('data-src') or img.get('data-lazy-src')) if img else None
  alliance=None
  # Alliance text commonly appears after the server-rank phrase; keep conservative.
  tail=txt[sm.end():]
  for token in re.findall(r'\b[A-Z0-9]{2,8}\b',tail):
   if token not in {'SERVER','GLOBAL','FILTER'}: alliance=token; break
  rank=int(sm.group(1))
  gr=global_pat.search(txt); fr=filter_pat.search(txt)
  rows.append({'uid':None,'name':name,'server':str(server),'alliance':alliance,'power':power_num(pm.group(1)),'rank':rank,'avatar':avatar,'source':url,'global_rank':int(gr.group(1)) if gr else None,'filter_rank':int(fr.group(1)) if fr else None})
 return dedupe(rows)

@app.post('/api/fetch')
def fetch(payload:FetchIn):
 server=str(payload.server)
 if not 1<=int(server)<=1000: raise HTTPException(400,'State/server must be 1–1000')
 url=SOURCE_TEMPLATE.format(server=server)
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36','Accept':'text/html,application/json;q=0.9,*/*;q=0.8'})
  with urllib.request.urlopen(req,timeout=30) as response:
   raw=response.read().decode('utf-8','replace'); rows=parse_players(raw,server,url)
   if not rows:
    title=re.search(r'<title[^>]*>(.*?)</title>',raw,re.I|re.S); msg=f'HTTP {response.status} page reached, but no player rows could be verified for State {server}. Parser/source mismatch or unavailable cached records. No import performed.'
    log(url,'empty',msg,0); return {'success':False,'url':url,'count':0,'message':msg,'preview':[],'state':server,'diagnostics':{'html_chars':len(raw),'content_type':response.headers.get('Content-Type','unknown'),'page_title':title.group(1).strip() if title else 'unknown','server_filter_format':'range (server-server)','parser':'ranking-card heuristic v8','server_phrase_hits':len(re.findall(r'server\s*'+re.escape(str(server)),raw,re.I)),'rank_phrase_hits':len(re.findall(r'global\s*#|server\s*#',raw,re.I)),'html_excerpt':raw[:900]}}
   c=db(); now=datetime.now(timezone.utc).isoformat(); n=0
   for r in rows:
    uid=r.get('uid') or ('source:'+server+':'+r['name'].lower().strip())
    c.execute('''INSERT INTO players(uid,name,server,alliance,power,rank,avatar,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,server=excluded.server,alliance=COALESCE(excluded.alliance,players.alliance),power=excluded.power,rank=COALESCE(excluded.rank,players.rank),avatar=COALESCE(excluded.avatar,players.avatar),source=excluded.source,updated_at=excluded.updated_at''',(uid,r['name'],server,r.get('alliance'),r['power'],r.get('rank'),r.get('avatar'),url,now)); n+=1
   c.commit(); c.close(); msg=f'Imported/updated {n} records for State {server}.'; log(url,'success',msg,n); return {'success':True,'url':url,'count':n,'message':msg,'preview':rows[:10],'state':server}
 except Exception as e:
  msg=f'{type(e).__name__}: {str(e)[:400]}'; log(url,'error',msg); raise HTTPException(502,detail={'url':url,'message':msg})
@app.get('/api/source')
def source(): return {'source_template':SOURCE_TEMPLATE}

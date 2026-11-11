import os
import sqlite3
import secrets
import hashlib
import hmac
import io
import re
import unicodedata
from pydantic import BaseModel, Field
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

DB = os.getenv("MV_DB", "mv_entregas.db")
PASSWORD = os.getenv("MV_PASSWORD")
if not PASSWORD:
    raise RuntimeError("Configure MV_PASSWORD no ambiente antes de iniciar.")
SECRET = os.getenv("MV_SESSION_SECRET") or secrets.token_hex(32)
app = FastAPI(title="MV Entregas")
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site="lax", https_only=os.getenv("MV_HTTPS") == "1")
templates = Jinja2Templates(directory="templates")

@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS drivers (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, plate TEXT NOT NULL,
      cpf TEXT NOT NULL UNIQUE, rate_cents INTEGER NOT NULL CHECK(rate_cents>=0)
    );
    CREATE TABLE IF NOT EXISTS deliveries (
      id INTEGER PRIMARY KEY, driver_id INTEGER NOT NULL REFERENCES drivers(id),
      day TEXT NOT NULL, packages INTEGER NOT NULL CHECK(packages>=0),
      rate_cents INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS ml_driver_links (
      source_name TEXT PRIMARY KEY,
      driver_id INTEGER NOT NULL REFERENCES drivers(id)
    );
    CREATE TABLE IF NOT EXISTS ml_progress (
      driver_id INTEGER NOT NULL REFERENCES drivers(id),
      day TEXT NOT NULL,
      processed INTEGER NOT NULL CHECK(processed>=0),
      total INTEGER NOT NULL CHECK(total>=0),
      failures INTEGER NOT NULL CHECK(failures>=0),
      packages INTEGER NOT NULL CHECK(packages>=0),
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(driver_id,day)
    );
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY, action TEXT NOT NULL, delivery_id INTEGER,
      details TEXT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    columns={r["name"] for r in c.execute("PRAGMA table_info(ml_progress)")}
    if "status" not in columns:
        c.execute("ALTER TABLE ml_progress ADD COLUMN status TEXT NOT NULL DEFAULT 'em_progresso'")
    if "finalized_at" not in columns:
        c.execute("ALTER TABLE ml_progress ADD COLUMN finalized_at TEXT")
    c.execute("""CREATE VIEW IF NOT EXISTS combined_deliveries AS
      SELECT driver_id,day,packages,rate_cents FROM deliveries
      UNION ALL
      SELECT m.driver_id,m.day,m.packages,d.rate_cents
      FROM ml_progress m JOIN drivers d ON d.id=m.driver_id""")

class MLDriver(BaseModel):
    nome: str = Field(min_length=3, max_length=160)
    processados: int = Field(ge=0, le=100000)
    total: int = Field(ge=0, le=100000)
    falhas: int = Field(ge=0, le=100000)
    status: str = Field(pattern="^(em_progresso|finalizado)$")

class MLBatch(BaseModel):
    motoristas: list[MLDriver] = Field(min_length=1,max_length=200)
    dia: date

def normalize_name(name):
    return " ".join(unicodedata.normalize("NFKD",name).casefold().split())

@app.post("/api/ml/sync")
def sync_ml(request:Request, payload:MLBatch):
    # O endpoint aceita somente chamadas originadas no proprio computador.
    if request.client is None or request.client.host not in ("127.0.0.1","::1"):
        raise HTTPException(403,"Somente acesso local")
    expected=os.getenv("MV_SYNC_TOKEN","")
    received=request.headers.get("X-MV-Sync-Token","")
    if len(expected)<24 or not hmac.compare_digest(expected,received):
        raise HTTPException(401,"Token de sincronizacao invalido")
    if payload.dia > date.today() or payload.dia < date.today()-timedelta(days=1):
        raise HTTPException(400,"Data da rota fora da janela permitida")
    day=str(payload.dia)
    updated=created=0
    with db() as c:
        existing=c.execute("SELECT id,name FROM drivers").fetchall()
        links={r["source_name"]:r["driver_id"] for r in c.execute("SELECT source_name,driver_id FROM ml_driver_links")}
        known={}
        for d in existing:
            known.setdefault(normalize_name(d["name"]),[]).append(d["id"])
        seen=set()
        for item in payload.motoristas:
            name=" ".join(item.nome.split())
            key=normalize_name(name)
            if key in seen or not key or item.processados>item.total or item.falhas>item.processados:
                raise HTTPException(400,"Dados de motorista inconsistentes")
            seen.add(key)
            matches=[links[key]] if key in links else known.get(key,[])
            if len(matches)>1:
                raise HTTPException(409,"Nome duplicado no cadastro: "+name)
            if matches:
                driver_id=matches[0]
            else:
                # Identificador interno provisório; nao e um CPF real.
                import uuid
                marker="ML-PENDENTE-"+uuid.uuid4().hex
                cur=c.execute("INSERT INTO drivers(name,plate,cpf,rate_cents) VALUES(?,?,?,0)",
                              (name,"PENDENTE",marker))
                driver_id=cur.lastrowid
                known[key]=[driver_id]
                created+=1
            c.execute("INSERT OR IGNORE INTO ml_driver_links(source_name,driver_id) VALUES(?,?)",(key,driver_id))
            packages=item.processados-item.falhas
            prior=c.execute("SELECT processed,total,failures,status FROM ml_progress WHERE driver_id=? AND day=?",(driver_id,day)).fetchone()
            # Uma rota finalizada nunca volta a 'em progresso' nem perde pacotes.
            if prior and prior["status"]=="finalizado" and item.status!="finalizado":
                continue
            if prior and item.processados < prior["processed"]:
                continue
            c.execute("""INSERT INTO ml_progress(driver_id,day,processed,total,failures,packages,status,finalized_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                         ON CONFLICT(driver_id,day) DO UPDATE SET
                         processed=excluded.processed,total=excluded.total,
                         failures=excluded.failures,packages=excluded.packages,
                         status=excluded.status,
                         finalized_at=CASE WHEN ml_progress.finalized_at IS NOT NULL
                           THEN ml_progress.finalized_at ELSE excluded.finalized_at END,
                         updated_at=CURRENT_TIMESTAMP""",
                      (driver_id,day,item.processados,item.total,item.falhas,packages,item.status,
                       datetime.now().isoformat(timespec="seconds") if item.status=="finalizado" else None))
            updated+=1
    return {"ok":True,"data":day,"motoristas":updated,"novos":created}

def auth(request):
    if not request.session.get("auth"):
        raise HTTPException(401, "Acesso não autorizado")

def csrf(request, token):
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "Token de segurança inválido")

def render(request, page, **ctx):
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(request, "index.html", {"page": page, "csrf": request.session["csrf"], **ctx})

def cents(value):
    from decimal import Decimal, InvalidOperation
    try:
        v = Decimal(value.replace(",", "."))
        n = int((v * 100).quantize(Decimal("1")))
        if n < 0: raise ValueError()
        return n
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Valor inválido")

def period_bounds(today, period):
    if period == "dia":
        return today, today
    if period == "semana":
        return today - timedelta(days=today.weekday()), today
    if period == "mes":
        return today.replace(day=1), today
    return today.replace(day=1 if today.day <= 15 else 16), today


@app.get("/", response_class=HTMLResponse)
def home(request: Request, periodo: str = "quinzena"):
    if not request.session.get("auth"):
        return render(request, "login")
    if periodo not in ("dia", "semana", "quinzena", "mes"):
        periodo = "quinzena"
    today = date.today()
    start, end = period_bounds(today, periodo)
    with db() as c:
        stats = {}
        for key in ("dia", "semana", "quinzena", "mes"):
            first, last = period_bounds(today, key)
            r = c.execute(
                "SELECT COALESCE(SUM(packages),0) n, COALESCE(SUM(packages*rate_cents),0) v "
                "FROM combined_deliveries WHERE day BETWEEN ? AND ?", (str(first), str(last))
            ).fetchone()
            stats[key] = dict(r)
        ranking = [dict(row) for row in c.execute(
            "SELECT d.name, COALESCE(SUM(e.packages),0) n, "
            "COALESCE(SUM(e.packages*e.rate_cents),0) v "
            "FROM drivers d LEFT JOIN combined_deliveries e ON e.driver_id=d.id "
            "AND e.day BETWEEN ? AND ? GROUP BY d.id ORDER BY n DESC,d.name",
            (str(start), str(end))
        ).fetchall()]
        daily = {row["day"]: dict(row) for row in c.execute(
            "SELECT day,SUM(packages) n,SUM(packages*rate_cents) v "
            "FROM combined_deliveries WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day",
            (str(start), str(end))
        ).fetchall()}
        active_drivers = c.execute(
            "SELECT COUNT(DISTINCT driver_id) n FROM combined_deliveries WHERE day BETWEEN ? AND ?",
            (str(start), str(end))
        ).fetchone()["n"]
    days = []
    current = start
    while current <= end:
        record = daily.get(str(current), {"n": 0, "v": 0})
        days.append({"label": current.strftime("%d/%m"), "n": record["n"], "v": record["v"]})
        current += timedelta(days=1)
    selected = stats[periodo]
    max_daily = max((row["n"] for row in days), default=0)
    max_driver = max((row["n"] for row in ranking), default=0)
    return render(
        request, "dashboard", stats=stats, ranking=ranking, days=days,
        periodo=periodo, selected=selected, active_drivers=active_drivers,
        max_daily=max_daily, max_driver=max_driver,
        period_label=f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"
    )


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        return render(request, "login", error="Senha incorreta")
    request.session.clear()
    request.session["auth"] = True
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/",303)

@app.post("/logout")
def logout(request: Request, token: str = Form(...)):
    auth(request); csrf(request,token)
    request.session.clear()
    return RedirectResponse("/",303)

@app.get("/motoristas", response_class=HTMLResponse)
def drivers(request: Request):
    auth(request)
    with db() as c:
        ds=c.execute("SELECT * FROM drivers ORDER BY name").fetchall()
        entries=c.execute("SELECT e.*,d.name FROM deliveries e JOIN drivers d ON d.id=e.driver_id ORDER BY e.day DESC,e.id DESC LIMIT 100").fetchall()
    return render(request,"motoristas",drivers=ds,entries=entries)

@app.post("/motoristas")
def add_driver(request: Request, token: str=Form(...), name: str=Form(...), plate: str=Form(...), cpf: str=Form(...), rate: str=Form(...)):
    auth(request); csrf(request,token)
    digits="".join(ch for ch in cpf if ch.isdigit())
    if len(digits)!=11 or not name.strip() or not plate.strip(): raise HTTPException(400,"Dados inválidos")
    with db() as c:
        try: c.execute("INSERT INTO drivers(name,plate,cpf,rate_cents) VALUES(?,?,?,?)",(name.strip(),plate.strip().upper(),digits,cents(rate)))
        except sqlite3.IntegrityError: raise HTTPException(400,"CPF já cadastrado")
    return RedirectResponse("/motoristas",303)

@app.post("/motoristas/{driver_id}/editar")
def edit_driver(request: Request, driver_id:int, token:str=Form(...), name:str=Form(...),plate:str=Form(...),cpf:str=Form(...),rate:str=Form(...)):
    auth(request);csrf(request,token)
    with db() as c:
        old=c.execute("SELECT cpf,plate FROM drivers WHERE id=?",(driver_id,)).fetchone()
        if not old:raise HTTPException(404,"Motorista não encontrado")
        provisional=old["cpf"].startswith("ML-PENDENTE-")
        digits="".join(ch for ch in cpf if ch.isdigit())
        # Permite manter o identificador provisório; nunca interpreta como CPF.
        if provisional and (cpf==old["cpf"] or not cpf.strip()):
            stored_cpf=old["cpf"]
        elif len(digits)==11:
            stored_cpf=digits
        else:
            raise HTTPException(400,"Informe CPF válido ou mantenha o cadastro provisório")
        plate_value=plate.strip().upper()
        if not name.strip() or not plate_value:
            raise HTTPException(400,"Nome e placa são obrigatórios")
        try:
            c.execute("UPDATE drivers SET name=?,plate=?,cpf=?,rate_cents=? WHERE id=?",
                      (name.strip(),plate_value,stored_cpf,cents(rate),driver_id))
        except sqlite3.IntegrityError:
            raise HTTPException(400,"CPF já cadastrado")
    return RedirectResponse("/motoristas",303)

@app.post("/entregas")
def add_delivery(request: Request,token:str=Form(...),driver_id:int=Form(...),day:date=Form(...),packages:int=Form(...)):
    auth(request);csrf(request,token)
    if packages<0:raise HTTPException(400,"Quantidade inválida")
    with db() as c:
        d=c.execute("SELECT rate_cents FROM drivers WHERE id=?",(driver_id,)).fetchone()
        if not d:raise HTTPException(404,"Motorista não encontrado")
        c.execute("INSERT INTO deliveries(driver_id,day,packages,rate_cents) VALUES(?,?,?,?)",(driver_id,str(day),packages,d["rate_cents"]))
    return RedirectResponse("/motoristas",303)

@app.post("/entregas/{entry_id}/editar")
def edit_delivery(request:Request,entry_id:int,token:str=Form(...),driver_id:int=Form(...),day:date=Form(...),packages:int=Form(...)):
    auth(request);csrf(request,token)
    if packages<0:raise HTTPException(400,"Quantidade inválida")
    with db() as c:
        old=c.execute("SELECT * FROM deliveries WHERE id=?",(entry_id,)).fetchone()
        driver=c.execute("SELECT rate_cents FROM drivers WHERE id=?",(driver_id,)).fetchone()
        if not old or not driver:raise HTTPException(404,"Registro não encontrado")
        rate=old["rate_cents"] if old["driver_id"]==driver_id else driver["rate_cents"]
        c.execute("UPDATE deliveries SET driver_id=?,day=?,packages=?,rate_cents=? WHERE id=?",(driver_id,str(day),packages,rate,entry_id))
        c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",("editar",entry_id,str(dict(old))))
    return RedirectResponse("/motoristas",303)

@app.post("/entregas/{entry_id}/excluir")
def delete_delivery(request:Request,entry_id:int,token:str=Form(...)):
    auth(request);csrf(request,token)
    with db() as c:
        old=c.execute("SELECT * FROM deliveries WHERE id=?",(entry_id,)).fetchone()
        if old:
            c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",("excluir",entry_id,str(dict(old))))
            c.execute("DELETE FROM deliveries WHERE id=?",(entry_id,))
    return RedirectResponse("/motoristas",303)

def report_data(start,end,ids):
    if end<start:raise HTTPException(400,"Período inválido")
    with db() as c:
        drivers=c.execute("SELECT id,name FROM drivers ORDER BY name").fetchall()
        selected=[d for d in drivers if not ids or d["id"] in ids]
        result=[]
        for d in selected:
            rows=c.execute("""SELECT x.day,x.n,x.v,COALESCE(m.failures,0) falhas,
CASE WHEN m.status='finalizado' THEN 'Finalizado'
WHEN m.status='em_progresso' THEN 'Em progresso' ELSE 'Manual' END status
FROM (SELECT day,SUM(packages) n,SUM(packages*rate_cents) v
FROM combined_deliveries WHERE driver_id=? AND day BETWEEN ? AND ? GROUP BY day) x
LEFT JOIN ml_progress m ON m.driver_id=? AND m.day=x.day
ORDER BY x.day""",(d["id"],start,end,d["id"])).fetchall()
            result.append((d,rows))
    return drivers,result

@app.get("/relatorios",response_class=HTMLResponse)
def reports(request:Request, start:str="",end:str="",motorista:list[int]|None=None):
    auth(request)
    today=str(date.today())
    start=start or today;end=end or today
    drivers,result=report_data(start,end,motorista or [])
    return render(request,"relatorios",drivers=drivers,result=result,start=start,end=end,selected=motorista or [])

@app.get("/relatorios/pdf")
def pdf(request:Request,start:str,end:str,motorista:list[int]|None=None):
    auth(request)
    _,result=report_data(start,end,motorista or [])
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf)
    styles=getSampleStyleSheet()
    story=[Paragraph("MV Entregas - Relatório de Entregas",styles["Title"]),Paragraph(f"Período: {start} a {end}",styles["Normal"]),Spacer(1,18)]
    grand_n=grand_v=0
    for i,(d,rows) in enumerate(result):
        if i:story.append(PageBreak())
        story.append(Paragraph(d["name"],styles["Heading2"]))
        data=[["Data","Entregues","Falhas","Status","Valor (R$)"]]
        n=v=failures=0
        for r in rows:
            n+=r["n"];v+=r["v"];failures+=r["falhas"]
            data.append([r["day"],str(r["n"]),str(r["falhas"]),r["status"],f'{r["v"]/100:.2f}'])
        data.append(["TOTAL",str(n),str(failures),"",f"{v/100:.2f}"])
        grand_n+=n;grand_v+=v
        t=Table(data,repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#ffe600")),("GRID",(0,0),(-1,-1),.4,colors.grey),("PADDING",(0,0),(-1,-1),8)]))
        story.append(t)
    story.extend([Spacer(1,20),Paragraph(f"Total geral: {grand_n} pacotes - R$ {grand_v/100:.2f}",styles["Heading2"])])
    doc.build(story);buf.seek(0)
    return StreamingResponse(buf,media_type="application/pdf",headers={"Content-Disposition":"attachment; filename=mv-entregas-relatorio.pdf"})

from backup_restore import install as install_backup_restore
install_backup_restore(app, DB, auth, csrf, render)

from historico import install as install_historico
install_historico(app, db, auth, csrf, render)

from busca_pacotes import install as install_busca_pacotes
install_busca_pacotes(app, auth, render)

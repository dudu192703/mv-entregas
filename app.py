import os
import sqlite3
import secrets
import hashlib
import hmac
import io
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
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY, action TEXT NOT NULL, delivery_id INTEGER,
      details TEXT, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

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
                "FROM deliveries WHERE day BETWEEN ? AND ?", (str(first), str(last))
            ).fetchone()
            stats[key] = dict(r)
        ranking = [dict(row) for row in c.execute(
            "SELECT d.name, COALESCE(SUM(e.packages),0) n, "
            "COALESCE(SUM(e.packages*e.rate_cents),0) v "
            "FROM drivers d LEFT JOIN deliveries e ON e.driver_id=d.id "
            "AND e.day BETWEEN ? AND ? GROUP BY d.id ORDER BY n DESC,d.name",
            (str(start), str(end))
        ).fetchall()]
        daily = {row["day"]: dict(row) for row in c.execute(
            "SELECT day,SUM(packages) n,SUM(packages*rate_cents) v "
            "FROM deliveries WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day",
            (str(start), str(end))
        ).fetchall()}
        active_drivers = c.execute(
            "SELECT COUNT(DISTINCT driver_id) n FROM deliveries WHERE day BETWEEN ? AND ?",
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
    digits="".join(ch for ch in cpf if ch.isdigit())
    if len(digits)!=11 or not name.strip() or not plate.strip():raise HTTPException(400,"Dados inválidos")
    with db() as c:
        try: c.execute("UPDATE drivers SET name=?,plate=?,cpf=?,rate_cents=? WHERE id=?",(name.strip(),plate.strip().upper(),digits,cents(rate),driver_id))
        except sqlite3.IntegrityError: raise HTTPException(400,"CPF já cadastrado")
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
            rows=c.execute("SELECT day,SUM(packages) n,SUM(packages*rate_cents) v FROM deliveries WHERE driver_id=? AND day BETWEEN ? AND ? GROUP BY day ORDER BY day",(d["id"],start,end)).fetchall()
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
        data=[["Data","Pacotes","Valor (R$)"]]
        n=v=0
        for r in rows:
            n+=r["n"];v+=r["v"]
            data.append([r["day"],str(r["n"]),f'{r["v"]/100:.2f}'])
        data.append(["TOTAL",str(n),f"{v/100:.2f}"])
        grand_n+=n;grand_v+=v
        t=Table(data,repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#ffe600")),("GRID",(0,0),(-1,-1),.4,colors.grey),("PADDING",(0,0),(-1,-1),8)]))
        story.append(t)
    story.extend([Spacer(1,20),Paragraph(f"Total geral: {grand_n} pacotes - R$ {grand_v/100:.2f}",styles["Heading2"])])
    doc.build(story);buf.seek(0)
    return StreamingResponse(buf,media_type="application/pdf",headers={"Content-Disposition":"attachment; filename=mv-entregas-relatorio.pdf"})

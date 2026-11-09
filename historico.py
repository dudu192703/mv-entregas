from datetime import date
from fastapi import Request, Form, HTTPException
from fastapi.responses import RedirectResponse

def install(app, db, auth, csrf, render):
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ml_history_overrides (
            driver_id INTEGER NOT NULL REFERENCES drivers(id),
            day TEXT NOT NULL,
            packages INTEGER,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(driver_id,day),
            CHECK(packages IS NULL OR packages>=0)
        )""")
        # A view respeita ajustes manuais mesmo depois de nova sincronizacao.
        c.execute("DROP VIEW IF EXISTS combined_deliveries")
        c.execute("""CREATE VIEW combined_deliveries AS
          SELECT driver_id,day,packages,rate_cents FROM deliveries
          UNION ALL
          SELECT m.driver_id,m.day,COALESCE(o.packages,m.packages),d.rate_cents
          FROM ml_progress m JOIN drivers d ON d.id=m.driver_id
          LEFT JOIN ml_history_overrides o ON o.driver_id=m.driver_id AND o.day=m.day
          WHERE COALESCE(o.deleted,0)=0""")

    @app.get("/historico")
    def history(request: Request, motorista: int = 0, inicio: str = "", fim: str = ""):
        auth(request)
        with db() as c:
            drivers = c.execute("SELECT id,name FROM drivers ORDER BY name").fetchall()
            filters = []
            args = []
            if motorista:
                filters.append("x.driver_id=?")
                args.append(motorista)
            if inicio:
                try: date.fromisoformat(inicio)
                except ValueError: raise HTTPException(400,"Data inicial invalida")
                filters.append("x.day>=?")
                args.append(inicio)
            if fim:
                try: date.fromisoformat(fim)
                except ValueError: raise HTTPException(400,"Data final invalida")
                filters.append("x.day<=?")
                args.append(fim)
            where = " WHERE " + " AND ".join(filters) if filters else ""
            entries = c.execute("""SELECT x.*,d.name,d.rate_cents driver_rate FROM (
                SELECT 'manual' source,id entry_id,driver_id,day,packages,rate_cents,0 deleted
                FROM deliveries
                UNION ALL
                SELECT 'ml' source,NULL entry_id,m.driver_id,m.day,
                COALESCE(o.packages,m.packages) packages,d.rate_cents,
                COALESCE(o.deleted,0) deleted
                FROM ml_progress m JOIN drivers d ON d.id=m.driver_id
                LEFT JOIN ml_history_overrides o ON o.driver_id=m.driver_id AND o.day=m.day
                ) x JOIN drivers d ON d.id=x.driver_id""" + where +
                " ORDER BY x.day DESC,d.name,x.source LIMIT 2000",args).fetchall()
        return render(request,"historico",drivers=drivers,entries=entries,
                      motorista=motorista,inicio=inicio,fim=fim)

    @app.post("/historico/editar")
    def edit_history(request: Request, token: str = Form(...), source: str = Form(...),
                     driver_id: int = Form(...), day: date = Form(...),
                     packages: int = Form(...), entry_id: int = Form(0)):
        auth(request); csrf(request,token)
        if packages<0 or packages>1000000:
            raise HTTPException(400,"Quantidade invalida")
        with db() as c:
            if source=="manual":
                old=c.execute("SELECT * FROM deliveries WHERE id=? AND driver_id=? AND day=?",
                              (entry_id,driver_id,str(day))).fetchone()
                if not old: raise HTTPException(404,"Lancamento nao encontrado")
                c.execute("UPDATE deliveries SET packages=? WHERE id=?",(packages,entry_id))
                c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",
                          ("editar_historico",entry_id,str(dict(old))))
            elif source=="ml":
                old=c.execute("SELECT packages FROM ml_progress WHERE driver_id=? AND day=?",
                              (driver_id,str(day))).fetchone()
                if not old: raise HTTPException(404,"Registro automatico nao encontrado")
                c.execute("""INSERT INTO ml_history_overrides(driver_id,day,packages,deleted)
                             VALUES(?,?,?,0) ON CONFLICT(driver_id,day)
                             DO UPDATE SET packages=excluded.packages,deleted=0,
                             updated_at=CURRENT_TIMESTAMP""",(driver_id,str(day),packages))
                c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",
                          ("editar_ml",None,f"driver={driver_id}, day={day}, anterior={old['packages']}, novo={packages}"))
            else:
                raise HTTPException(400,"Origem invalida")
        return RedirectResponse("/historico",303)

    @app.post("/historico/excluir")
    def delete_history(request: Request, token: str = Form(...), source: str = Form(...),
                       driver_id: int = Form(...), day: date = Form(...),
                       entry_id: int = Form(0)):
        auth(request); csrf(request,token)
        with db() as c:
            if source=="manual":
                old=c.execute("SELECT * FROM deliveries WHERE id=? AND driver_id=? AND day=?",
                              (entry_id,driver_id,str(day))).fetchone()
                if not old: raise HTTPException(404,"Lancamento nao encontrado")
                c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",
                          ("excluir_historico",entry_id,str(dict(old))))
                c.execute("DELETE FROM deliveries WHERE id=?",(entry_id,))
            elif source=="ml":
                old=c.execute("SELECT packages FROM ml_progress WHERE driver_id=? AND day=?",
                              (driver_id,str(day))).fetchone()
                if not old: raise HTTPException(404,"Registro automatico nao encontrado")
                c.execute("""INSERT INTO ml_history_overrides(driver_id,day,packages,deleted)
                             VALUES(?,?,NULL,1) ON CONFLICT(driver_id,day)
                             DO UPDATE SET packages=NULL,deleted=1,
                             updated_at=CURRENT_TIMESTAMP""",(driver_id,str(day)))
                c.execute("INSERT INTO audit(action,delivery_id,details) VALUES(?,?,?)",
                          ("excluir_ml",None,f"driver={driver_id}, day={day}, anterior={old['packages']}"))
            else:
                raise HTTPException(400,"Origem invalida")
        return RedirectResponse("/historico",303)

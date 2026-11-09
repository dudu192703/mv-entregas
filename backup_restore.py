
import io
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from fastapi import Request, Form, File, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse

def install(app, db_path, auth, csrf, render):
    def snapshot(path):
        source = sqlite3.connect(db_path, timeout=30)
        try:
            target = sqlite3.connect(str(path))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    @app.get("/configuracoes")
    def settings(request: Request, mensagem: str = ""):
        auth(request)
        return render(request, "configuracoes", mensagem=mensagem)

    @app.post("/configuracoes/baixar")
    def download(request: Request, token: str = Form(...)):
        auth(request)
        csrf(request, token)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mv_entregas.db"
            snapshot(path)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(path, "mv_entregas.db")
            output.seek(0)
        filename = "mv-entregas-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"
        return StreamingResponse(output, media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="' + filename + '"',
                     "Cache-Control": "no-store"})

    @app.post("/configuracoes/restaurar")
    async def restore(request: Request, token: str = Form(...),
                      confirmar: str = Form(...), arquivo: UploadFile = File(...)):
        auth(request)
        csrf(request, token)
        if confirmar != "RESTAURAR":
            raise HTTPException(400, "Digite RESTAURAR para confirmar")
        raw = await arquivo.read(25 * 1024 * 1024 + 1)
        if len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413, "Arquivo ZIP excede 25 MB")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != "mv_entregas.db":
                    raise ValueError("ZIP deve conter somente mv_entregas.db")
                if members[0].file_size > 100 * 1024 * 1024:
                    raise ValueError("Banco excede 100 MB")
                with tempfile.TemporaryDirectory() as directory:
                    incoming = Path(directory) / "incoming.db"
                    with archive.open(members[0]) as src, incoming.open("wb") as dest:
                        import shutil
                        shutil.copyfileobj(src, dest)
                    if incoming.stat().st_size > 100 * 1024 * 1024:
                        raise ValueError("Banco excede 100 MB")
                    with sqlite3.connect(str(incoming)) as conn:
                        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Banco corrompido")
                        expected = {
                            "drivers": {"id", "name", "plate", "cpf", "rate_cents"},
                            "deliveries": {"id", "driver_id", "day", "packages", "rate_cents"},
                            "ml_driver_links": {"source_name", "driver_id"},
                            "ml_progress": {"driver_id", "day", "processed", "total", "failures", "packages", "status", "finalized_at"},
                            "audit": {"id", "action", "delivery_id", "details", "at"},
                        }
                        for table, columns in expected.items():
                            actual = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
                            if not columns.issubset(actual):
                                raise ValueError("Banco incompativel: " + table)
                        if conn.execute("PRAGMA foreign_key_check").fetchone():
                            raise ValueError("Referencias invalidas")
                    backup_dir = Path(os.getenv("MV_BACKUP_DIR", str(Path(db_path).resolve().parent / "backups")))
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    previous = backup_dir / ("antes-restauracao-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
                    snapshot(previous)
                    source = sqlite3.connect(str(incoming))
                    destination = sqlite3.connect(db_path, timeout=30)
                    try:
                        source.backup(destination)
                    except Exception:
                        recovery = sqlite3.connect(str(previous))
                        try:
                            recovery.backup(destination)
                        finally:
                            recovery.close()
                        raise
                    finally:
                        source.close()
                        destination.close()
        except (ValueError, OSError, zipfile.BadZipFile, sqlite3.DatabaseError) as error:
            raise HTTPException(400, "Nao foi possivel restaurar: " + str(error))
        return RedirectResponse("/configuracoes?mensagem=Dados+restaurados", status_code=303)

"""Busca de pacotes no banco persistente do servidor MV Entregas."""
import re
import sqlite3
from fastapi import Request

def install(app, auth, render, database_path):
    @app.get("/buscar-pacote")
    def buscar_pacote(request: Request, id: str = ""):
        auth(request)
        shipment_id = id.strip()
        resultados = []
        mensagem = ""
        if shipment_id:
            if not re.fullmatch(r"\d{8,20}", shipment_id):
                mensagem = "Digite um ID de pacote válido (8 a 20 números)."
            else:
                try:
                    with sqlite3.connect(database_path, timeout=10) as conn:
                        conn.row_factory = sqlite3.Row
                        registros = conn.execute(
                            """SELECT shipment_id, address, zone_external_id, zone_name,
                                      stop_sequence, total_shipments
                               FROM synced_packages WHERE shipment_id = ?
                               ORDER BY last_seen DESC LIMIT 10""",
                            (shipment_id,),
                        ).fetchall()
                        resultados = [dict(r) for r in registros]
                    if not resultados:
                        mensagem = "Nenhum pacote encontrado com esse ID. Verifique a sincronização."
                except sqlite3.Error:
                    mensagem = "Não foi possível consultar os pacotes sincronizados."
        return render(request, "buscar_pacote", pacote_id=shipment_id,
                      resultados=resultados, mensagem=mensagem)

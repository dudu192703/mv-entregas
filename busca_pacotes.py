"""Busca de pacotes no banco local do coletor de zonas."""
import os
import re
import sqlite3
from pathlib import Path
from fastapi import Request

def install(app, auth, render):
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
                caminho = Path(os.getenv("MV_ZONAS_DB", str(Path(__file__).resolve().parent / "zonas_coletadas.db")))
                if not caminho.is_file():
                    mensagem = "Banco de zonas não encontrado neste servidor."
                else:
                    try:
                        # Modo somente leitura: a busca nunca altera os dados coletados.
                        uri = caminho.resolve().as_uri() + "?mode=ro"
                        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                            conn.row_factory = sqlite3.Row
                            registros = conn.execute(
                                """SELECT shipment_id, address, zone_external_id, zone_name,
                                          stop_sequence, total_shipments
                                   FROM markers WHERE shipment_id = ?
                                   ORDER BY last_seen DESC LIMIT 10""",
                                (shipment_id,),
                            ).fetchall()
                            resultados = [dict(r) for r in registros]
                        if not resultados:
                            mensagem = "Nenhum pacote encontrado com esse ID."
                    except sqlite3.Error:
                        mensagem = "Não foi possível consultar o banco de zonas."
        return render(request, "buscar_pacote", pacote_id=shipment_id,
                      resultados=resultados, mensagem=mensagem)

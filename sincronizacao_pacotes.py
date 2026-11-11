"""Importação autenticada de pacotes coletados no PC para o banco do servidor."""
import hmac
import os
import re
import sqlite3
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

class Pacote(BaseModel):
    shipment_id: str = Field(pattern=r"^\d{8,20}$")
    planned_route_id: str = Field(max_length=160)
    address: str | None = Field(default=None, max_length=1500)
    zone_external_id: str | None = Field(default=None, max_length=160)
    zone_name: str | None = Field(default=None, max_length=300)
    stop_sequence: int | None = None
    total_shipments: int | None = None
    last_seen: str | None = Field(default=None, max_length=40)

class Lote(BaseModel):
    pacotes: list[Pacote] = Field(min_length=1, max_length=200)

def install(app, database_path):
    with sqlite3.connect(database_path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS synced_packages (
            shipment_id TEXT NOT NULL, planned_route_id TEXT NOT NULL,
            address TEXT, zone_external_id TEXT, zone_name TEXT,
            stop_sequence INTEGER, total_shipments INTEGER,
            last_seen TEXT,
            PRIMARY KEY (shipment_id, planned_route_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_synced_packages_shipment ON synced_packages(shipment_id)")

    @app.post("/api/pacotes/sync")
    def sync_packages(request: Request, lote: Lote):
        expected = os.getenv("MV_PACKAGE_SYNC_TOKEN", "")
        provided = request.headers.get("X-MV-Package-Token", "")
        if len(expected) < 32 or not hmac.compare_digest(expected, provided):
            raise HTTPException(401, "Token de sincronização inválido")
        with sqlite3.connect(database_path, timeout=20) as con:
            for item in lote.pacotes:
                d = item.model_dump()
                # Never erase a previously captured address with an empty one.
                con.execute("""INSERT INTO synced_packages
                  (shipment_id,planned_route_id,address,zone_external_id,zone_name,
                   stop_sequence,total_shipments,last_seen)
                  VALUES (:shipment_id,:planned_route_id,:address,:zone_external_id,:zone_name,
                          :stop_sequence,:total_shipments,:last_seen)
                  ON CONFLICT(shipment_id,planned_route_id) DO UPDATE SET
                   address=COALESCE(NULLIF(excluded.address,''),synced_packages.address),
                   zone_external_id=excluded.zone_external_id,
                   zone_name=excluded.zone_name,
                   stop_sequence=excluded.stop_sequence,
                   total_shipments=excluded.total_shipments,
                   last_seen=excluded.last_seen""",d)
        return {"ok": True, "recebidos": len(lote.pacotes)}

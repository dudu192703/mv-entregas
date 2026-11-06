# MV Entregas

Sistema local de gerenciamento de entregas e pagamentos de motoristas.

## Instalação (Windows)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MV_PASSWORD='defina-uma-senha-forte'
uvicorn app:app --host 127.0.0.1 --port 8000
```

Abra http://127.0.0.1:8000. Para acesso pela rede/túnel, configure HTTPS e autenticação adicional antes de expor o serviço.

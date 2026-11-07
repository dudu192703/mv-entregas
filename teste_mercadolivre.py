"""Teste supervisionado do acesso ao painel do Mercado Livre.

Não envia credenciais nem grava dados do painel. O login é feito pelo usuário.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "https://envios.mercadolivre.com.br/logistics/dist-ops/reports/monitoring"
PROFILE = Path(__file__).resolve().parent / ".ml-browser-profile"

def main():
    print("Abrindo navegador. Entre no Mercado Livre normalmente, se necessário.")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print("Navegue até 'Progresso por motorista'.")
        input("Quando os cartões dos motoristas aparecerem, volte aqui e pressione ENTER...")
        print("Título:", page.title())
        print("Endereço atual:", page.url)
        print("Prévia do texto visível (até 2000 caracteres):")
        print(page.locator("body").inner_text(timeout=15000)[:2000])
        print("Teste concluído. Nenhuma entrega foi modificada.")
        context.close()

if __name__ == "__main__":
    main()

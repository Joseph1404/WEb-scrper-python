from bs4 import BeautifulSoup


MAX_PAGE_BYTES = 5 * 1024 * 1024
BROWSER_TIMEOUT = 30_000


def buscar_pagina_com_javascript(url, user_agent, validar_destino=None):
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "A coleta JavaScript exige Playwright. Instale as dependências do navegador."
        ) from exc

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        try:
            pagina = navegador.new_page(
                user_agent=user_agent,
                java_script_enabled=True,
            )
            pagina.set_default_timeout(BROWSER_TIMEOUT)
            def filtrar_requisicao(rota):
                if rota.request.resource_type in {"image", "media", "font"}:
                    rota.abort()
                    return
                if validar_destino and rota.request.url.startswith(("http://", "https://")):
                    try:
                        validar_destino(rota.request.url)
                    except ValueError:
                        rota.abort()
                        return
                rota.continue_()

            pagina.route("**/*", filtrar_requisicao)
            pagina.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            try:
                pagina.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            html = pagina.content()
        finally:
            navegador.close()

    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError("A página renderizada excede o limite de 5 MB para coleta.")

    return BeautifulSoup(html, "html.parser")

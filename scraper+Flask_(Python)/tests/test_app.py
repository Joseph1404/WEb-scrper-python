import os
import tempfile
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from bs4 import BeautifulSoup

# A variável precisa ser definida antes de importar App, pois a aplicação
# inicializa o banco durante a importação. Nunca use o banco local do projeto.
TEST_DATABASE_DIR = tempfile.TemporaryDirectory(prefix="webharbor-tests-")
TEST_DATABASE = Path(TEST_DATABASE_DIR.name) / "webharbor-test.sqlite3"
os.environ["WEBHARBOR_DATABASE"] = str(TEST_DATABASE)
os.environ["WEBHARBOR_ENV"] = "development"
os.environ["WEBHARBOR_CSRF_ENABLED"] = "false"
os.environ["WEBHARBOR_PUBLIC_SIGNUP"] = "false"

import App


def test_testes_usam_banco_temporario_isolado():
    import database

    assert database.DATABASE_PATH == TEST_DATABASE
    assert database.DATABASE_PATH.parent == Path(TEST_DATABASE_DIR.name)


def autenticar_cliente_no_teste(client):
    import database

    usuario_id = database.criar_usuario(
        f"coletor-{uuid4().hex}@example.com", "senha-segura-123"
    )
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = usuario_id
    return usuario_id


def test_normalizar_url_adiciona_https_quando_falta_scheme():
    assert App.normalizar_url(" exemplo.com ") == "https://exemplo.com"


def test_validar_url_rejeita_url_invalida():
    assert App.validar_url("   ") == "Informe uma URL válida."


def test_validar_destino_seguro_rejeita_endereco_local():
    import pytest

    with pytest.raises(ValueError, match="endereços locais"):
        App.validar_destino_seguro("http://127.0.0.1:5000")


def test_cadastro_login_e_logout():
    import database

    client = App.app.test_client()
    email = f"cliente-{uuid4().hex}@example.com"
    senha = "senha-segura-123"

    database.criar_usuario(email, senha)
    resposta = client.post("/login", data={"email": email, "senha": senha})
    assert resposta.status_code == 302
    conta = client.get("/conta")
    assert conta.status_code == 200
    assert email in conta.get_data(as_text=True)


def test_csrf_protege_formularios_quando_habilitado(monkeypatch):
    monkeypatch.setattr(App, "CSRF_HABILITADO", True)
    monkeypatch.setattr(App, "CADASTRO_PUBLICO_HABILITADO", True)
    client = App.app.test_client()

    bloqueada = client.post(
        "/cadastro", data={"email": "csrf@example.com", "senha": "senha-segura-123"}
    )
    assert bloqueada.status_code == 400

    client.get("/cadastro")
    with client.session_transaction() as sessao:
        token = sessao["csrf_token"]
    aceita = client.post(
        "/cadastro",
        data={
            "email": f"csrf-{uuid4().hex}@example.com",
            "senha": "senha-segura-123",
            "csrf_token": token,
        },
    )
    assert aceita.status_code == 302


def test_usuario_pode_excluir_conta_com_senha():
    import database

    email = f"excluir-{uuid4().hex}@example.com"
    usuario_id = database.criar_usuario(email, "senha-segura-123")
    client = App.app.test_client()
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = usuario_id

    resposta = client.post("/conta/excluir", data={"senha": "errada"})
    assert resposta.status_code == 400
    assert database.buscar_usuario(usuario_id) is not None

    resposta = client.post("/conta/excluir", data={"senha": "senha-segura-123"})
    assert resposta.status_code == 302
    assert database.buscar_usuario(usuario_id) is None
    with client.session_transaction() as sessao:
        assert "usuario_id" not in sessao


def test_nova_coleta_mantem_usuario_autenticado():
    client = App.app.test_client()
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = 123
        sessao["etapa"] = "dados"

    resposta = client.get("/limpar")

    assert resposta.status_code == 302
    with client.session_transaction() as sessao:
        assert sessao["usuario_id"] == 123
        assert "etapa" not in sessao


def test_login_com_senha_temporaria_exige_troca(monkeypatch):
    import database

    email = f"temporario-{uuid4().hex}@example.com"
    usuario_id = database.criar_usuario(
        email,
        "senha-temporaria",
        plano="basico",
        senha_temporaria=True,
        limite_coletas=25,
    )
    client = App.app.test_client()

    resposta = client.post(
        "/login", data={"email": email, "senha": "senha-temporaria"}
    )
    assert resposta.status_code == 302
    assert resposta.location.endswith("/alterar-senha")
    assert client.get("/").location.endswith("/alterar-senha")

    resposta = client.post(
        "/alterar-senha",
        data={"senha": "senha-definitiva", "confirmacao": "senha-definitiva"},
    )
    assert resposta.status_code == 302
    usuario = database.buscar_usuario(usuario_id)
    assert usuario["senha_temporaria"] == 0
    assert usuario["limite_coletas"] == 25


def test_administrador_atualiza_limite_do_cliente(monkeypatch):
    import database

    admin_email = f"admin-{uuid4().hex}@example.com"
    admin_id = database.criar_usuario(admin_email, "senha-admin-123")
    cliente_id = database.criar_usuario(
        f"cliente-limite-{uuid4().hex}@example.com", "senha-cliente"
    )
    monkeypatch.setenv("WEBHARBOR_ADMIN_EMAIL", admin_email)
    client = App.app.test_client()
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = admin_id

    resposta = client.post(
        f"/admin/usuarios/{cliente_id}/limite",
        data={"limite_coletas": "42", "plano": "basico"},
    )

    assert resposta.status_code == 302
    cliente = database.buscar_usuario(cliente_id)
    assert cliente["limite_coletas"] == 42
    assert cliente["plano"] == "basico"
    assert resposta.location.endswith("/admin/usuarios")


def test_painel_admin_visual_e_protegido(monkeypatch):
    import database

    admin_email = f"painel-admin-{uuid4().hex}@example.com"
    admin_id = database.criar_usuario(admin_email, "senha-admin-123")
    monkeypatch.setenv("WEBHARBOR_ADMIN_EMAIL", admin_email)

    cliente = App.app.test_client()
    assert cliente.get("/admin/usuarios").status_code == 403

    with cliente.session_transaction() as sessao:
        sessao["usuario_id"] = admin_id
    resposta = cliente.get("/admin/usuarios")
    body = resposta.get_data(as_text=True)
    assert resposta.status_code == 200
    assert "Gerencie seus clientes." in body
    assert "Novo cliente" in body
    assert "Clientes cadastrados" in body


def test_admin_exibe_mensagem_de_entrega_apos_criar_cliente(monkeypatch):
    import database

    admin_email = f"entrega-admin-{uuid4().hex}@example.com"
    admin_id = database.criar_usuario(admin_email, "senha-admin-123")
    monkeypatch.setenv("WEBHARBOR_ADMIN_EMAIL", admin_email)
    monkeypatch.setattr(App, "URL_PUBLICA", "https://app.webharbor.test")
    client = App.app.test_client()
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = admin_id

    resposta = client.post(
        "/admin/usuarios",
        data={
            "email": "novo-cliente@example.com",
            "senha": "temporaria-123",
            "plano": "basico",
            "limite_coletas": "100",
        },
    )
    corpo = resposta.get_data(as_text=True)

    assert resposta.status_code == 200
    assert "Mensagem para entregar no Fiverr" in corpo
    assert "https://app.webharbor.test/login" in corpo
    assert "novo-cliente@example.com" in corpo
    assert "temporaria-123" in corpo


def test_telas_de_autenticacao_usam_tema_webharbor():
    client = App.app.test_client()

    login = client.get("/login").get_data(as_text=True)
    assert client.get("/cadastro").status_code == 404
    assert "Criar conta" not in login
    assert "WebHarbor" in login
    assert "auth-card" in login
    assert "primary-button" in login
    assert "styles.css" in login


def test_cadastro_publico_pode_ser_ativado_no_futuro(monkeypatch):
    monkeypatch.setattr(App, "CADASTRO_PUBLICO_HABILITADO", True)
    client = App.app.test_client()

    resposta = client.get("/cadastro")

    assert resposta.status_code == 200
    assert "Criar conta" in resposta.get_data(as_text=True)


def test_coleta_autenticada_respeita_limite_mensal(monkeypatch):
    import database

    usuario_id = database.criar_usuario(
        f"limite-{uuid4().hex}@example.com", "senha-segura-123"
    )
    with database.conectar() as conexao:
        conexao.execute(
            "UPDATE usuarios SET coletas_mes = 5 WHERE id = ?", (usuario_id,)
        )
        conexao.commit()

    client = App.app.test_client()
    with client.session_transaction() as sessao:
        sessao["usuario_id"] = usuario_id
        sessao["etapa"] = "dados"
        sessao["site"] = "https://example.com"

    resposta = client.post(
        "/",
        data={"modo": "conversa", "mensagem": "todos os dados"},
        follow_redirects=True,
    )

    assert resposta.status_code == 200
    assert "limite mensal" in resposta.get_data(as_text=True)


def test_uso_mensal_eh_zerado_ao_virar_o_ciclo(monkeypatch):
    import database

    usuario_id = database.criar_usuario(
        f"reset-mensal-{uuid4().hex}@example.com", "senha-segura-123"
    )
    with database.conectar() as conexao:
        conexao.execute(
            "UPDATE usuarios SET coletas_mes = 5, mes_referencia = '2026-08' WHERE id = ?",
            (usuario_id,),
        )
        conexao.commit()

    monkeypatch.setattr(database, "mes_atual", lambda: "2026-09")

    usuario = database.buscar_usuario(usuario_id)

    assert usuario["coletas_mes"] == 0
    assert usuario["mes_referencia"] == "2026-09"
    assert database.pode_coletar(usuario)


def test_buscar_pagina_ignora_content_length_invalido(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.headers = {"Content-Length": "abc"}
            self.ok = True
            self.text = "<html><body>ok</body></html>"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=64 * 1024):
            yield b"<html><body>ok</body></html>"

    dummy = Mock(return_value=FakeResponse())
    monkeypatch.setattr(App.sessao, "get", dummy)
    monkeypatch.setattr(App, "validar_destino_seguro", lambda url: None)

    soup = App.buscar_pagina("https://example.com")

    assert soup.title is None or "body" in str(soup)


def test_extrair_dados_automaticos_aceita_html_complexo(monkeypatch):
    html = '''
    <html>
      <body>
        <div class="product-card product-card--featured">
          <h2 class="title">Notebook 1</h2>
          <p class="price">R$ 2000</p>
        </div>
        <div class="product-card product-card--featured">
          <h2 class="title">Notebook 2</h2>
          <p class="price">R$ 2500</p>
        </div>
      </body>
    </html>
    '''
    monkeypatch.setattr(App, "buscar_pagina", lambda url: BeautifulSoup(html, "html.parser"))

    dados = App.extrair_dados_automaticos("https://example.com")

    assert "product-card" in dados["seletor_itens"]
    assert len(dados["resultados"]) == 2
    assert dados["resultados"][0]["titulo"] == "Notebook 1"


def test_template_permite_mensagem_textual_na_etapa_de_dados():
    html = Path("templates/index.html").read_text()

    assert 'data-chat-stage="{{ etapa }}"' in html
    assert "const chatStage = form.dataset.chatStage || 'site';" in html
    assert "if (chatStage === 'site' && (!value || !isValidUrl))" in html
    assert 'placeholder="Ex.: título e preço"' in html
    assert 'Descreva os dados que deseja extrair.' in html
    assert "resultado.get(coluna, '')" in html
    assert "Coleta dinâmica: JavaScript executado" in html
    assert "Transforme páginas públicas em dados utilizáveis." in html
    assert 'name="incluir_imagens"' in html
    assert "toggle-track" in html
    assert 'id="results-search"' in html
    assert 'id="results-pagination"' in html
    assert "Página " in html
    assert 'role="status"' in html
    assert 'role="alert"' in html
    assert 'aria-busy="false"' in html
    assert "Analisando a página e descobrindo os campos..." in html


def test_historico_reinicia_coleta_com_url_selecionada():
    client = App.app.test_client()
    autenticar_cliente_no_teste(client)
    with client.session_transaction() as sessao:
        sessao["etapa"] = "dados"
        sessao["site"] = "https://old.example"

    resposta = client.get(
        "/limpar?url=https%3A%2F%2Fbooks.toscrape.com%2F",
        follow_redirects=True,
    )

    assert resposta.status_code == 200
    assert 'value="https://books.toscrape.com/"' in resposta.get_data(as_text=True)
    with client.session_transaction() as sessao:
        assert sessao.get("etapa", "site") == "site"
        assert "site" not in sessao


def test_fluxo_conversa_site_para_dados_envia_mensagem_textual():
    client = App.app.test_client()
    autenticar_cliente_no_teste(client)

    resp1 = client.post('/', data={'modo': 'conversa', 'mensagem': 'https://books.toscrape.com/'}, follow_redirects=True)
    assert resp1.status_code == 200
    assert 'data-chat-stage="dados"' in resp1.get_data(as_text=True)

    resp2 = client.post('/', data={'modo': 'conversa', 'mensagem': 'titulo e preço'}, follow_redirects=True)
    assert resp2.status_code == 200
    body = resp2.get_data(as_text=True)
    assert 'Analisei a página' in body
    assert 'result-count' in body


def test_coletor_redireciona_visitante_sem_login():
    client = App.app.test_client()

    resp = client.get('/')

    assert resp.status_code == 302
    assert resp.location.endswith('/login')


def test_fluxo_conversa_com_mensagem_vazia_nao_quebra():
    client = App.app.test_client()
    autenticar_cliente_no_teste(client)

    resp = client.post('/', data={'modo': 'conversa', 'mensagem': '   '}, follow_redirects=True)

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'site' in body.lower() or 'URL' in body or 'mensagem' in body.lower()


def test_texto_do_campo_ignora_status_de_disponibilidade():
    html = '''
    <article class="product_pod">
      <h3>Livro</h3>
      <p class="availability">In stock</p>
      <p>Descrição real do livro</p>
    </article>
    '''
    soup = BeautifulSoup(html, "html.parser")
    item = soup.select_one("article")

    texto = App.texto_do_campo(item, "p")

    assert texto == "Descrição real do livro"


def test_extracao_automatica_nao_expoe_status_como_campo(monkeypatch):
        html = '''
        <article class="product_pod">
            <h3>Livro 1</h3>
            <p class="availability">In stock</p>
        </article>
        <article class="product_pod">
            <h3>Livro 2</h3>
            <p class="availability">In stock</p>
        </article>
        '''
        monkeypatch.setattr(
                App,
                "buscar_pagina",
                lambda url: BeautifulSoup(html, "html.parser"),
        )

        dados = App.extrair_dados_automaticos("https://example.com")

        assert "texto" not in dados["campos"]
        assert all("In stock" not in resultado.values() for resultado in dados["resultados"])
        assert dados["resultados"] == [{"titulo": "Livro 1"}, {"titulo": "Livro 2"}]


def test_extracao_automatica_usa_fallback_javascript(monkeypatch):
    html_renderizado = '''
    <article class="product_pod">
      <h3>Produto carregado por JavaScript</h3>
      <p class="price">R$ 49,90</p>
    </article>
    <article class="product_pod">
      <h3>Outro produto</h3>
      <p class="price">R$ 59,90</p>
    </article>
    '''
    monkeypatch.setattr(
        App,
        "buscar_pagina",
        lambda url: BeautifulSoup("<html><body></body></html>", "html.parser"),
    )
    monkeypatch.setattr(
        App,
        "buscar_pagina_com_javascript",
        lambda url, user_agent, validar_destino=None: BeautifulSoup(html_renderizado, "html.parser"),
    )

    dados = App.extrair_dados_automaticos("https://example.com")

    assert len(dados["resultados"]) == 2
    assert dados["resultados"][0]["titulo"] == "Produto carregado por JavaScript"
    assert dados["modo_coleta"] == "javascript"


def test_extracao_zap_nao_confunde_fotos_e_preco_com_texto():
        html = '''
        <a class="olx-core-card overflow-hidden text-neutral-120">
            <div class="photo text-neutral-70">+ 15 fotos</div>
            <h2>Apartamento para comprar com 60 m² em Maceió</h2>
            <p class="flex flex-wrap items-baseline">
                <span class="typo-title-small font-bold text-nowrap">R$ 495.000</span>
            </p>
        </a>
        <a class="olx-core-card overflow-hidden text-neutral-120">
            <div class="photo text-neutral-70">+ 8 fotos</div>
            <h2>Casa para comprar com 100 m² em Maceió</h2>
            <p class="flex flex-wrap items-baseline">
                <span class="typo-title-small font-bold text-nowrap">R$ 700.000</span>
            </p>
        </a>
        '''

        dados = App.extrair_dados_da_soup(
                "https://www.zapimoveis.com.br/venda/imoveis/al+maceio/",
                BeautifulSoup(html, "html.parser"),
        )

        assert dados["campos"] == {
                "titulo": "h2",
                "preco": "[class*=typo-title-small][class*=font-bold]",
        }
        assert dados["resultados"][0] == {
                "titulo": "Apartamento para comprar com 60 m² em Maceió",
                "preco": "R$ 495.000",
        }


def test_extracao_de_imagem_e_opcional():
        html = '''
        <article class="product-card">
            <h2>Produto</h2>
            <img src="/images/produto.jpg" alt="Imagem do produto">
        </article>
        <article class="product-card">
            <h2>Outro produto</h2>
            <img data-src="/images/outro.jpg" alt="Outro produto">
        </article>
        '''
        soup = BeautifulSoup(html, "html.parser")

        sem_imagens = App.extrair_dados_da_soup(
                "https://example.com/catalogo/", soup, incluir_imagens=False
        )
        com_imagens = App.extrair_dados_da_soup(
                "https://example.com/catalogo/", soup, incluir_imagens=True
        )

        assert "imagem_url" not in sem_imagens["resultados"][0]
        assert com_imagens["resultados"][0]["imagem_url"] == "https://example.com/images/produto.jpg"
        assert com_imagens["resultados"][0]["imagem_alt"] == "Imagem do produto"
        assert com_imagens["resultados"][1]["imagem_url"] == "https://example.com/images/outro.jpg"

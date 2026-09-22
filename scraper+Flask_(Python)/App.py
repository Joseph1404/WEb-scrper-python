from datetime import datetime
from io import BytesIO
import ipaddress
import os
import re
import socket
import sqlite3
from uuid import uuid4

from flask import Flask, redirect, render_template, render_template_string, request, send_file, session, url_for
from openpyxl import Workbook
from openpyxl.styles import Font
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from coleta_javascript import buscar_pagina_com_javascript
from database import (
    autenticar_usuario,
    buscar_usuario,
    criar_usuario,
    inicializar_banco,
    listar_usuarios,
    pode_coletar,
    registrar_coleta,
    atualizar_limite_usuario,
    atualizar_senha,
    excluir_usuario,
)

app = Flask(__name__, static_folder="templates/styles", static_url_path="/static")
app.secret_key = os.environ.get("WEBHARBOR_SECRET_KEY") or os.urandom(32)
inicializar_banco()

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 15
MAX_PAGE_BYTES = 5 * 1024 * 1024

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "DNT": "1",
    "Connection": "keep-alive",
}

sessao = requests.Session()
sessao.headers.update(DEFAULT_HEADERS)
retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
sessao.mount("http://", HTTPAdapter(max_retries=retry))
sessao.mount("https://", HTTPAdapter(max_retries=retry))

relatorios = {}
PAGINAS_CACHE = {}
ROBOTS_CACHE = {}


def normalizar_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = f"https://{url}"
    return url


def escapar_css(valor):
    if not valor:
        return ""
    substituicoes = {
        "\\": "\\\\",
        ".": "\\.",
        "#": "\\#",
        ":": "\\:",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        ">": "\\>",
        "+": "\\+",
        "~": "\\~",
        " ": "\\ ",
        ",": "\\,",
        "'": "\\'",
        '"': '\\"',
    }
    return "".join(substituicoes.get(caractere, caractere) for caractere in valor)


def validar_url(url):
    url = normalizar_url(url)
    url_parsed = urlparse(url)
    if not url_parsed.netloc:
        return "Informe uma URL válida."
    if url_parsed.scheme not in ("http", "https"):
        return "A URL deve começar com http:// ou https://."
    return None


def validar_destino_seguro(url):
    parsed = urlparse(normalizar_url(url))
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("A URL não possui um domínio válido.")
    if hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("Por segurança, endereços locais não podem ser coletados.")
    try:
        enderecos = {
            info[4][0]
            for info in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("Não foi possível resolver o domínio informado.") from exc
    for endereco in enderecos:
        ip = ipaddress.ip_address(endereco)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Por segurança, não é permitido coletar endereços locais ou de rede internos.")


def verificar_robots(url):
    url = normalizar_url(url)
    if url in ROBOTS_CACHE:
        return ROBOTS_CACHE[url]

    robots_url = urljoin(url, "/robots.txt")
    try:
        response = sessao.get(
            robots_url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if response.ok:
            regras = response.text.splitlines()
            from urllib.robotparser import RobotFileParser

            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(regras)
            pode = parser.can_fetch(USER_AGENT, url)
            ROBOTS_CACHE[url] = pode
            if not pode:
                raise ValueError("O robots.txt do site não permite essa coleta.")
            return True
    except requests.RequestException:
        pass

    ROBOTS_CACHE[url] = True
    return True


def buscar_pagina(url):
    url = normalizar_url(url)
    validar_destino_seguro(url)
    if url in PAGINAS_CACHE:
        return PAGINAS_CACHE[url]

    verificar_robots(url)
    try:
        response = sessao.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=False,
        )
        if getattr(response, "is_redirect", False) or getattr(response, "is_permanent_redirect", False):
            destino = response.headers.get("Location", "")
            validar_destino_seguro(urljoin(url, destino))
            raise ValueError(
                "O site solicitou um redirecionamento. Abra a URL final diretamente para continuar."
            )
        response.raise_for_status()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            raise ValueError(
                "Este site bloqueou acessos automáticos (Cloudflare/anti-bot). "
                "O navegador consegue carregar, mas um scraper sem suporte de JS é bloqueado."
            ) from exc
        raise
    except requests.RequestException as exc:
        raise ValueError(f"Não foi possível acessar a URL: {exc}") from exc

    tamanho = response.headers.get("Content-Length")
    if tamanho:
        try:
            tamanho_bytes = int(str(tamanho).replace(",", ""))
        except (TypeError, ValueError):
            tamanho_bytes = None
        if tamanho_bytes is not None and tamanho_bytes > MAX_PAGE_BYTES:
            raise ValueError("A página excede o limite de 5 MB para coleta.")

    conteudo = bytearray()
    try:
        for bloco in response.iter_content(chunk_size=64 * 1024):
            if not bloco:
                continue
            conteudo.extend(bloco)
            if len(conteudo) > MAX_PAGE_BYTES:
                raise ValueError("A página excede o limite de 5 MB para coleta.")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    texto = bytes(conteudo)
    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            texto_decodificado = texto.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        texto_decodificado = texto.decode("utf-8", errors="replace")

    soup = BeautifulSoup(texto_decodificado, "html.parser")
    PAGINAS_CACHE[url] = soup
    return soup


def descobrir_seletor_itens(soup):
    candidatos = []
    tags = ["article", "li", "tr", "div", "section", "a"]
    for elemento in soup.find_all(tags):
        classes = elemento.get("class", [])
        if not classes:
            continue
        classes_validas = [escapar_css(classe) for classe in classes[:3] if classe]
        if not classes_validas:
            continue
        seletor = f"{elemento.name}.{'.'.join(classes_validas)}"
        try:
            quantidade = len(soup.select(seletor))
        except Exception:
            continue
        if quantidade < 2 or quantidade > 1000:
            continue

        nomes = " ".join(classes).lower()
        bonus = sum(
            termo in nomes
            for termo in (
                "price", "preço", "produto", "product", "title", "rating",
                "item", "card", "quote", "citation", "post", "article", "name",
            )
        )
        bonus += sum(termo in nomes for termo in ("quote", "card", "item", "product", "name")) * 3
        bonus -= sum(termo in nomes for termo in ("container", "wrapper", "row", "header", "footer", "toolbar")) * 4
        bonus += {"article": 5, "li": 3, "tr": 2, "div": 0, "section": 2, "a": 1}.get(elemento.name, 0)
        texto_elemento = elemento.get_text(" ", strip=True)
        if re.search(r"(?:R\$|US\$|€|£)\s*[\d.,]+", texto_elemento):
            bonus += 8
        if any(len(candidato.get_text(" ", strip=True)) >= 20 for candidato in elemento.find_all(["h1", "h2", "h3", "h4"])):
            bonus += 6
        if elemento.name == "a" and any("card" in classe.lower() for classe in classes):
            bonus += 5
        tem_preco = bool(re.search(r"(?:R\$|US\$|€|£)\s*[\d.,]+", texto_elemento))
        tem_titulo = any(
            len(candidato.get_text(" ", strip=True)) >= 20
            for candidato in elemento.find_all(["h1", "h2", "h3", "h4"])
        )
        if re.match(r"\+\s*\d+\s+fotos?\b", texto_elemento, re.IGNORECASE) and not (tem_preco and tem_titulo):
            bonus -= 10
        candidatos.append((bonus, quantidade, seletor))

    if candidatos:
        candidatos.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return candidatos[0][2]

    for tag in ("article", "section", "tr", "li", "div"):
        if len(soup.select(tag)) >= 2:
            return tag
    return None


def primeiro_seletor(elemento, seletores):
    for seletor in seletores:
        if elemento.select_one(seletor):
            return seletor
    return None


def texto_eh_status(texto):
    if not isinstance(texto, str):
        return False
    texto = re.sub(r"\s+", " ", texto).strip().lower()
    if not texto:
        return False
    padroes = (
        "in stock",
        "out of stock",
        "available",
        "unavailable",
        "sold out",
        "disponível",
        "indisponível",
        "em estoque",
        "fora de estoque",
    )
    return texto in padroes or texto.startswith("in stock ") or texto.startswith("out of stock ") or texto.startswith("em estoque ") or texto.startswith("fora de estoque ")


def texto_eh_preco(texto):
    if not isinstance(texto, str):
        return False
    return bool(re.fullmatch(r"\s*(?:R\$|US\$|€|£)\s*[\d.,]+\s*", texto))


def texto_do_campo(elemento, seletor):
    if not seletor:
        return ""
    campos = elemento.select(seletor)
    if not campos:
        return ""

    for campo in campos:
        texto = (
            campo.get("title", "").strip()
            or campo.get("alt", "").strip()
            or campo.get_text(" ", strip=True)
        )
        if not isinstance(texto, str):
            continue
        texto = re.sub(r"\s+", " ", texto).strip()
        if not texto or texto_eh_status(texto):
            continue
        return texto

    return ""


def extrair_dados_da_imagem(elemento, url):
    imagem = elemento.select_one("img")
    if not imagem:
        return {}

    origem = next(
        (
            imagem.get(nome, "").strip()
            for nome in ("data-src", "data-lazy-src", "data-original", "src")
            if imagem.get(nome, "").strip()
        ),
    )
    if not origem:
        srcset = imagem.get("srcset", "").strip()
        origem = srcset.split(",")[0].strip().split(" ")[0] if srcset else ""

    dados = {}
    if origem and not origem.startswith("data:"):
        dados["imagem_url"] = urljoin(url, origem)
    alt = imagem.get("alt", "").strip()
    if alt:
        dados["imagem_alt"] = alt
    return dados


def analisar_estrutura(soup, seletor_itens):
    itens = soup.select(seletor_itens) if seletor_itens else []
    if not itens:
        return [], {}
    primeiro = itens[0]
    campos = {
        "titulo": primeiro_seletor(
            primeiro, ["h1", "h2", "h3", "h4", "a[title]", ".title"]
        ),
        "autor": primeiro_seletor(
            primeiro, [".author", "[class*=author]", "[class*=autor]"]
        ),
        "preco": primeiro_seletor(
            primeiro,
            [
                ".price_color",
                ".price",
                "[class*=price]",
                "[class*=preco]",
                "[class*=typo-title-small][class*=font-bold]",
                "[data-cy*=price]",
                "[data-cy*=preco]",
                "[data-cy='rp-cardProperty-price-txt']",
                "[data-cy='property-price']",
            ],
        ),
        "classificacao": primeiro_seletor(
            primeiro, [".star-rating", ".rating", "[class*=rating]", "[class*=avaliacao]"]
        ),
        "descricao": primeiro_seletor(
            primeiro, [".description", "[class*=description]", "[class*=descricao]"]
        ),
        "texto": primeiro_seletor(
            primeiro,
            [
                ".description",
                ".text",
                "[class*=description]",
                "p:not([class*=price]):not([class*=rating]):not([class*=preco]):not([class*=availability]):not([class*=stock]):not([class*=instock]):not([class*=status]):not([class*=text-])",
            ],
        ),
    }
    campos = {nome: seletor for nome, seletor in campos.items() if seletor}
    if "texto" in campos and texto_eh_preco(texto_do_campo(primeiro, campos["texto"])):
        campos.pop("texto")
    return itens, campos


def extrair_dados_da_soup(url, soup, modo_coleta="estatica", incluir_imagens=False):
    titulo_pagina = soup.title.get_text(" ", strip=True) if soup.title else url
    seletor_itens = descobrir_seletor_itens(soup)
    if not seletor_itens:
        raise ValueError("Não encontrei uma estrutura repetida de itens nessa página.")

    itens, campos = analisar_estrutura(soup, seletor_itens)
    resultados = []
    for item in itens:
        linha = {}
        for nome, seletor in campos.items():
            valor = texto_do_campo(item, seletor)
            if valor:
                linha[nome] = valor
        if incluir_imagens:
            linha.update(extrair_dados_da_imagem(item, url))

        if linha:
            resultados.append(linha)

    if not resultados:
        raise ValueError("A estrutura foi encontrada, mas nenhum dado pôde ser extraído.")

    campos_validos = {
        nome: seletor
        for nome, seletor in campos.items()
        if any(resultado.get(nome) for resultado in resultados)
    }
    if incluir_imagens:
        if any(resultado.get("imagem_url") for resultado in resultados):
            campos_validos["imagem_url"] = "img"
        if any(resultado.get("imagem_alt") for resultado in resultados):
            campos_validos["imagem_alt"] = "img[alt]"
    resultados = [
        {
            nome: resultado[nome]
            for nome in campos_validos
            if resultado.get(nome)
        }
        for resultado in resultados
    ]
    resultados = [resultado for resultado in resultados if resultado]

    if not resultados or not campos_validos:
        raise ValueError("A estrutura foi encontrada, mas nenhum dado pôde ser extraído.")

    return {
        "site": url,
        "titulo_pagina": titulo_pagina,
        "modo_coleta": modo_coleta,
        "incluir_imagens": incluir_imagens,
        "seletor_itens": seletor_itens,
        "campos": campos_validos,
        "resultados": resultados,
    }


def extrair_dados_automaticos(url, incluir_imagens=False):
    soup = buscar_pagina(url)
    try:
        return extrair_dados_da_soup(url, soup, "estatica", incluir_imagens)
    except ValueError as erro_estatico:
        try:
            soup_renderizado = buscar_pagina_com_javascript(url, USER_AGENT)
        except RuntimeError as erro_javascript:
            raise ValueError(
                f"{erro_estatico} A tentativa com JavaScript não está disponível: {erro_javascript}"
            ) from erro_javascript
        return extrair_dados_da_soup(url, soup_renderizado, "javascript", incluir_imagens)


def criar_excel(dados):
    workbook = Workbook()
    planilha = workbook.active
    planilha.title = "Dados extraídos"
    colunas = list(dados["resultados"][0].keys())
    planilha.append(colunas)
    for celula in planilha[1]:
        celula.font = Font(bold=True)
    for resultado in dados["resultados"]:
        planilha.append([resultado.get(coluna, "") for coluna in colunas])
    for coluna in planilha.columns:
        letra = coluna[0].column_letter
        planilha.column_dimensions[letra].width = min(
            max(len(str(celula.value or "")) for celula in coluna) + 2, 60
        )

    arquivo = BytesIO()
    workbook.save(arquivo)
    arquivo.seek(0)
    return arquivo


def guardar_relatorio(dados):
    token = uuid4().hex
    arquivo = criar_excel(dados)
    relatorios[token] = arquivo.getvalue()
    return token


def registrar_url_historico(url):
    url = normalizar_url(url)
    historico = session.get("historico_urls", [])
    historico = [item for item in historico if item != url]
    historico.insert(0, url)
    session["historico_urls"] = historico[:6]
    return session["historico_urls"]


def usuario_atual():
    usuario_id = session.get("usuario_id")
    return buscar_usuario(usuario_id) if usuario_id else None


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    erro = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        if "@" not in email or len(senha) < 8:
            erro = "Informe um e-mail válido e uma senha com pelo menos 8 caracteres."
        else:
            try:
                session["usuario_id"] = criar_usuario(email, senha)
                return redirect(url_for("index"))
            except ValueError as exc:
                erro = str(exc)
    return render_template("auth.html", modo="cadastro", erro=erro)


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        usuario = autenticar_usuario(request.form.get("email") or "", request.form.get("senha") or "")
        if usuario:
            session["usuario_id"] = usuario["id"]
            if usuario.get("senha_temporaria"):
                return redirect(url_for("alterar_senha"))
            return redirect(url_for("index"))
        erro = "E-mail ou senha inválidos."
    return render_template("auth.html", modo="login", erro=erro)


@app.route("/logout")
def logout():
    session.pop("usuario_id", None)
    return redirect(url_for("index"))


@app.route("/alterar-senha", methods=["GET", "POST"])
def alterar_senha():
    usuario = usuario_atual()
    if not usuario:
        return redirect(url_for("login"))
    erro = None
    if request.method == "POST":
        senha = request.form.get("senha") or ""
        confirmacao = request.form.get("confirmacao") or ""
        if len(senha) < 8:
            erro = "A nova senha precisa ter pelo menos 8 caracteres."
        elif senha != confirmacao:
            erro = "A confirmação da senha não confere."
        else:
            atualizar_senha(usuario["id"], senha)
            return redirect(url_for("index"))
    return render_template("auth.html", modo="troca_senha", erro=erro)


def administrador_atual():
    usuario = usuario_atual()
    email_admin = (os.environ.get("WEBHARBOR_ADMIN_EMAIL") or "").strip().lower()
    if usuario and email_admin and usuario["email"] == email_admin:
        return usuario
    return None


@app.route("/admin/usuarios", methods=["GET", "POST"])
def admin_usuarios():
    administrador = administrador_atual()
    if not administrador:
        return "Acesso administrativo não configurado ou não autorizado.", 403
    erro = None
    mensagem = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        plano = request.form.get("plano") or "gratuito"
        try:
            limite = int(request.form.get("limite_coletas") or 0)
            novo_id = criar_usuario(email, senha, plano, senha_temporaria=True, limite_coletas=limite)
            mensagem = f"Cliente criado. ID: {novo_id}. Solicite a troca da senha no primeiro acesso."
        except (ValueError, sqlite3.Error) as exc:
            erro = str(exc)
    return render_template(
        "admin.html",
        administrador=administrador,
        usuarios=listar_usuarios(),
        erro=erro,
        mensagem=mensagem,
    )


@app.route("/admin/usuarios/<int:usuario_id>/limite", methods=["POST"])
def admin_atualizar_limite(usuario_id):
    if not administrador_atual():
        return "Acesso administrativo não configurado ou não autorizado.", 403
    try:
        limite = int(request.form.get("limite_coletas") or 0)
        plano = request.form.get("plano") or None
        atualizar_limite_usuario(usuario_id, limite, plano)
    except (ValueError, sqlite3.Error) as exc:
        return str(exc), 400
    return redirect(url_for("conta"))


@app.route("/conta")
def conta():
    usuario = usuario_atual()
    if not usuario:
        return redirect(url_for("login"))
    limite = usuario.get("limite_coletas", 5)
    return render_template("account.html", usuario=usuario, limite=limite)


@app.route("/conta/excluir", methods=["POST"])
def excluir_conta():
    usuario = usuario_atual()
    if not usuario:
        return redirect(url_for("login"))
    senha = request.form.get("senha") or ""
    if not excluir_usuario(usuario["id"], senha):
        return render_template(
            "account.html",
            usuario=usuario,
            limite=usuario.get("limite_coletas", 5),
            erro_exclusao="Senha incorreta. A conta não foi excluída.",
        ), 400
    session.clear()
    return redirect(url_for("index"))


def extrair_valor_generico(elemento, seletor):
    if not seletor:
        return None

    campo = elemento.select_one(seletor)
    if not campo:
        return None

    texto = campo.get_text(" ", strip=True)
    if texto:
        return texto

    return (
        campo.get("title", "").strip()
        or campo.get("alt", "").strip()
        or None
    )


@app.route("/", methods=["GET", "POST"])
def index():
    usuario = usuario_atual()
    if usuario and usuario.get("senha_temporaria"):
        return redirect(url_for("alterar_senha"))
    etapa = session.get("etapa", "site")
    historico_urls = session.get("historico_urls", [])
    resultados = []
    erro = None
    tipo_dado = "generico"
    formulario = {
        "url": "",
        "seletor": "",
        "campo_seletor": "",
    }

    if request.method == "POST" and request.form.get("modo") == "conversa":
        mensagem = (request.form.get("mensagem") or "").strip()
        incluir_imagens = request.form.get("incluir_imagens") == "on"
        session["incluir_imagens"] = incluir_imagens
        historico = session.get("historico", [])
        if historico is None:
            historico = []
        if mensagem:
            historico.append({"autor": "cliente", "texto": mensagem})

        if not mensagem:
            resposta = "Digite uma URL ou descreva o dado que deseja extrair."
            historico.append({"autor": "webharbor", "texto": resposta})
            session["historico"] = historico
            return render_template(
                "index.html",
                historico=historico,
                resultados=[],
                historico_urls=session.get("historico_urls", []),
                etapa=session.get("etapa", "site"),
            )

        if session.get("etapa", "site") == "site":
            url = normalizar_url(mensagem)
            erro_url = validar_url(url)
            if erro_url:
                resposta = erro_url
                historico.append({"autor": "webharbor", "texto": resposta})
                session["historico"] = historico
                return render_template(
                    "index.html",
                    historico=historico,
                    resultados=[],
                    historico_urls=session.get("historico_urls", []),
                    etapa=session.get("etapa", "site"),
                )

            try:
                soup = buscar_pagina(url)
                seletor_itens = descobrir_seletor_itens(soup)
                if not seletor_itens:
                    raise ValueError("Não encontrei uma estrutura repetida de itens nessa página.")
                _, campos = analisar_estrutura(soup, seletor_itens)
                registrar_url_historico(url)
                session["site"] = url
                session["estrutura"] = {
                    "seletor_itens": seletor_itens,
                    "campos": campos,
                }
                session["etapa"] = "dados"
                campos_encontrados = ", ".join(campos) or "texto"
                resposta = (
                    f"Analisei o site e encontrei os campos: {campos_encontrados}. "
                    "Quais dados você deseja extrair? Escreva, por exemplo: título e preço."
                )
            except (requests.RequestException, ValueError) as erro:
                resposta = f"Não consegui analisar esse site: {erro}"
            historico.append({"autor": "webharbor", "texto": resposta})
            session["historico"] = historico
            return render_template(
                "index.html",
                historico=historico,
                resultados=[],
                historico_urls=session.get("historico_urls", []),
                etapa=session.get("etapa", "site"),
            )

        try:
            url = session.get("site")
            if not url:
                raise ValueError("Ainda não há um site analisado. Envie a URL do site primeiro.")
            usuario = usuario_atual()
            if not pode_coletar(usuario):
                raise ValueError("Seu limite mensal de coletas foi atingido. Escolha um plano com limite maior.")
            dados = extrair_dados_automaticos(
                url,
                incluir_imagens=session.get("incluir_imagens", False),
            )
            pedido = mensagem.lower()
            sinonimos = {
                "titulo": ("titulo", "título", "nome"),
                "autor": ("autor", "autora", "author"),
                "preco": ("preco", "preço", "valor", "custo"),
                "classificacao": ("classificacao", "classificação", "nota", "avaliacao", "avaliação"),
                "descricao": ("descricao", "descrição", "detalhes"),
                "texto": ("texto", "conteudo", "conteúdo", "informação", "informacoes", "informações"),
            }
            campos_pedidos = {
                campo
                for campo, palavras in sinonimos.items()
                if any(palavra in pedido for palavra in palavras)
            }
            pediu_todos = "todos" in pedido or "tudo" in pedido
            if pediu_todos:
                campos_pedidos = set(dados["campos"])
            elif not campos_pedidos:
                session["etapa"] = "dados"
                resposta = (
                    "Dados não encontrados no site. Informe um campo válido "
                    "ou escreva 'todos os dados'."
                )
                historico.append({"autor": "webharbor", "texto": resposta})
                session["historico"] = historico
                return render_template(
                    "index.html",
                    historico=historico,
                    resultados=[],
                    historico_urls=session.get("historico_urls", []),
                    etapa=session.get("etapa", "site"),
                )
            dados["resultados"] = [
                {campo: resultado[campo] for campo in campos_pedidos if campo in resultado}
                for resultado in dados["resultados"]
            ]
            dados["resultados"] = [resultado for resultado in dados["resultados"] if resultado]
            if not dados["resultados"]:
                raise ValueError("Não encontrei os campos pedidos nos itens do site.")
            token = guardar_relatorio(dados)
            if usuario:
                registrar_coleta(usuario["id"], dados)
            registrar_url_historico(url)
            session["etapa"] = "site"
            session["ultimo_relatorio"] = token
            resultados = dados["resultados"]
            resposta = (
                f"Analisei a página, encontrei {len(resultados)} registros e gerei o relatório. "
                f"Seletor dos itens: {dados['seletor_itens']}."
            )
            historico.append({"autor": "webharbor", "texto": resposta})
            session["historico"] = historico
            return render_template(
                "index.html",
                historico=historico,
                resultados=resultados,
                analise=dados,
                download_url=f"/relatorio/{token}",
                historico_urls=session.get("historico_urls", []),
                etapa=session.get("etapa", "site"),
            )
        except (requests.RequestException, ValueError) as erro:
            session["etapa"] = "dados"
            resposta = f"Não consegui concluir a coleta: {erro}"
            historico.append({"autor": "webharbor", "texto": resposta})
            session["historico"] = historico
            return render_template(
                "index.html",
                historico=historico,
                resultados=[],
                historico_urls=session.get("historico_urls", []),
                etapa=session.get("etapa", "site"),
            )

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        seletor = request.form.get("seletor", "").strip()
        campo_seletor = request.form.get("campo_seletor", "").strip()
        tipo_dado = request.form.get("tipo_dado", "generico")
        formulario = {
            "url": url,
            "seletor": seletor,
            "campo_seletor": campo_seletor,
        }

        url_parsed = urlparse(url)
        if not url_parsed.scheme or not url_parsed.netloc:
            erro = "Informe uma URL válida."
        elif url_parsed.scheme not in ("http", "https"):
            erro = "A URL deve começar com http:// ou https://."
        elif not seletor:
            erro = "Informe o seletor dos itens."
        elif tipo_dado != "livros" and not campo_seletor:
            erro = "Informe o seletor do campo."
        else:
            try:
                response = sessao.get(
                    url,
                    headers={"User-Agent": "ScraperPratica/1.0"},
                    timeout=15,
                )
                response.raise_for_status()
                registrar_url_historico(url)
                soup = BeautifulSoup(response.text, "html.parser")
                elementos = soup.select(seletor)

                if not elementos:
                    erro = f"Nenhum elemento encontrado com o seletor '{seletor}'."
                elif tipo_dado == "livros":
                    for item in elementos:
                        titulo = item.select_one("h3 a") or item.select_one("h2 a") or item.select_one("a.title")
                        preco = item.select_one("p.price_color, .price_color, .price")
                        classificacao = item.select_one("p.star-rating, .star-rating")

                        if not any([titulo, preco, classificacao]):
                            continue

                        if titulo:
                            titulo_texto = (
                                titulo.get("title", "").strip()
                                or titulo.get_text(" ", strip=True)
                            )
                        else:
                            imagem = item.select_one("img[alt]")
                            titulo_texto = imagem.get("alt", "").strip() if imagem else "Título não encontrado"
                        titulo_texto = titulo_texto or "Título não encontrado"
                        preco_texto = preco.get_text(" ", strip=True) if preco else "Preço não encontrado"

                        if classificacao:
                            classes = classificacao.get("class", [])
                            classificacao_texto = next(
                                (classe.capitalize() for classe in classes if classe.lower() in {"one", "two", "three", "four", "five"}),
                                "Classificação não encontrada",
                            )
                        else:
                            classificacao_texto = "Classificação não encontrada"

                        resultados.append(
                            {
                                "titulo": titulo_texto,
                                "preco": preco_texto,
                                "classificacao": classificacao_texto,
                            }
                        )

                    if not resultados:
                        erro = "Nenhum dado de livro extraído. Verifique o seletor dos itens."
                else:
                    for elem in elementos:
                        valor = extrair_valor_generico(elem, campo_seletor)
                        if valor:
                            resultados.append({"dado": valor})

                    if not resultados:
                        erro = "Nenhum dado extraído. Verifique os seletores."

            except requests.RequestException as e:
                erro = f"Erro ao acessar a URL: {str(e)}"
            except Exception as e:
                erro = f"Erro ao processar: {str(e)}"

    return render_template(
        "index.html",
        resultados=resultados,
        erro=erro,
        tipo_dado=tipo_dado,
        formulario=formulario,
        historico=session.get("historico", []),
        historico_urls=session.get("historico_urls", []),
        etapa=session.get("etapa", "site"),
    )


@app.route("/relatorio/<token>")
def baixar_relatorio(token):
    conteudo = relatorios.get(token)
    if not conteudo:
        return "Relatório não encontrado ou expirado.", 404
    return send_file(
        BytesIO(conteudo),
        as_attachment=True,
        download_name=f"webharbor-{datetime.now():%Y%m%d-%H%M}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/limpar")
def limpar_conversa():
    url = (request.args.get("url") or "").strip()
    session.clear()
    if url:
        return redirect(url_for("index", url=url))
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
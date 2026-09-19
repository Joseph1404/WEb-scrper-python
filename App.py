from datetime import datetime
from io import BytesIO
import os
from uuid import uuid4

from flask import Flask, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook
from openpyxl.styles import Font
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__, static_folder="templates/styles", static_url_path="/static")
app.secret_key = os.environ.get("WEBHARBOR_SECRET_KEY") or os.urandom(32)

USER_AGENT = "ScraperPratica/1.0"
REQUEST_TIMEOUT = 15
MAX_PAGE_BYTES = 5 * 1024 * 1024

sessao = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
sessao.mount("http://", HTTPAdapter(max_retries=retry))
sessao.mount("https://", HTTPAdapter(max_retries=retry))

relatorios = {}


def normalizar_url(url):
    url = url.strip()
    if not urlparse(url).scheme:
        url = f"https://{url}"
    return url


def validar_url(url):
    url_parsed = urlparse(url)
    if not url_parsed.netloc:
        return "Informe uma URL válida."
    if url_parsed.scheme not in ("http", "https"):
        return "A URL deve começar com http:// ou https://."
    return None


def verificar_robots(url):
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
            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError("O robots.txt do site não permite essa coleta.")
    except requests.RequestException:
        pass


def buscar_pagina(url):
    verificar_robots(url)
    response = sessao.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        stream=True,
    )
    response.raise_for_status()
    tamanho = response.headers.get("Content-Length")
    if tamanho and int(tamanho) > MAX_PAGE_BYTES:
        raise ValueError("A página excede o limite de 5 MB para coleta.")

    conteudo = bytearray()
    for bloco in response.iter_content(chunk_size=64 * 1024):
        conteudo.extend(bloco)
        if len(conteudo) > MAX_PAGE_BYTES:
            raise ValueError("A página excede o limite de 5 MB para coleta.")
    return BeautifulSoup(bytes(conteudo), "html.parser")


def descobrir_seletor_itens(soup):
    candidatos = []
    for elemento in soup.find_all(["article", "li", "tr", "div"]):
        classes = elemento.get("class", [])
        if not classes:
            continue
        seletor = f"{elemento.name}.{'.'.join(classes[:2])}"
        quantidade = len(soup.select(seletor))
        if quantidade < 2 or quantidade > 1000:
            continue

        nomes = " ".join(classes).lower()
        bonus = sum(
            termo in nomes
            for termo in (
                "price", "preço", "produto", "product", "title", "rating",
                "item", "card", "quote", "citation", "post", "article",
            )
        )
        bonus += sum(termo in nomes for termo in ("quote", "card", "item", "product")) * 3
        bonus -= sum(termo in nomes for termo in ("container", "wrapper", "row", "header", "footer")) * 4
        bonus += {"article": 5, "li": 3, "tr": 2, "div": 0}.get(elemento.name, 0)
        candidatos.append((bonus, quantidade, seletor))

    if candidatos:
        candidatos.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return candidatos[0][2]

    for tag in ("article", "tr", "li"):
        if len(soup.select(tag)) >= 2:
            return tag
    return None


def primeiro_seletor(elemento, seletores):
    for seletor in seletores:
        if elemento.select_one(seletor):
            return seletor
    return None


def texto_do_campo(elemento, seletor):
    if not seletor:
        return ""
    campo = elemento.select_one(seletor)
    if not campo:
        return ""
    return (
        campo.get("title", "").strip()
        or campo.get("alt", "").strip()
        or campo.get_text(" ", strip=True)
    )


def analisar_estrutura(soup, seletor_itens):
    itens = soup.select(seletor_itens)
    primeiro = itens[0]
    campos = {
        "titulo": primeiro_seletor(
            primeiro, ["h1", "h2", "h3", "h4", "a[title]", ".title"]
        ),
        "autor": primeiro_seletor(
            primeiro, [".author", "[class*=author]", "[class*=autor]"]
        ),
        "preco": primeiro_seletor(
            primeiro, [".price_color", ".price", "[class*=price]", "[class*=preco]"]
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
                ".text",
                "[class*=text]",
                "p:not([class*=price]):not([class*=rating]):not([class*=preco])",
            ],
        ),
    }
    return itens, {nome: seletor for nome, seletor in campos.items() if seletor}


def extrair_dados_automaticos(url):
    soup = buscar_pagina(url)
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

        if linha:
            resultados.append(linha)

    if not resultados:
        raise ValueError("A estrutura foi encontrada, mas nenhum dado pôde ser extraído.")

    return {
        "site": url,
        "titulo_pagina": titulo_pagina,
        "seletor_itens": seletor_itens,
        "campos": campos,
        "resultados": resultados,
    }


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
    resultados = []
    erro = None
    tipo_dado = "generico"
    formulario = {
        "url": "",
        "seletor": "",
        "campo_seletor": "",
    }

    if request.method == "POST" and request.form.get("modo") == "conversa":
        mensagem = request.form.get("mensagem", "").strip()
        historico = session.get("historico", [])
        historico.append({"autor": "cliente", "texto": mensagem})

        if session.get("etapa", "site") == "site":
            url = normalizar_url(mensagem)
            erro_url = validar_url(url)
            if erro_url:
                resposta = erro_url
                historico.append({"autor": "webharbor", "texto": resposta})
                session["historico"] = historico
                return render_template("index.html", historico=historico, resultados=[])

            try:
                soup = buscar_pagina(url)
                seletor_itens = descobrir_seletor_itens(soup)
                if not seletor_itens:
                    raise ValueError("Não encontrei uma estrutura repetida de itens nessa página.")
                _, campos = analisar_estrutura(soup, seletor_itens)
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
            return render_template("index.html", historico=historico, resultados=[])

        try:
            url = session["site"]
            dados = extrair_dados_automaticos(url)
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
                campos_pedidos = set(dados["resultados"][0])
            elif not campos_pedidos:
                session["etapa"] = "dados"
                resposta = (
                    "Dados não encontrados no site. Informe um campo válido "
                    "ou escreva 'todos os dados'."
                )
                historico.append({"autor": "webharbor", "texto": resposta})
                session["historico"] = historico
                return render_template("index.html", historico=historico, resultados=[])
            dados["resultados"] = [
                {campo: resultado[campo] for campo in campos_pedidos if campo in resultado}
                for resultado in dados["resultados"]
            ]
            dados["resultados"] = [resultado for resultado in dados["resultados"] if resultado]
            if not dados["resultados"]:
                raise ValueError("Não encontrei os campos pedidos nos itens do site.")
            token = guardar_relatorio(dados)
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
            )
        except (requests.RequestException, ValueError) as erro:
            session["etapa"] = "dados"
            resposta = f"Não consegui concluir a coleta: {erro}"
            historico.append({"autor": "webharbor", "texto": resposta})
            session["historico"] = historico
            return render_template("index.html", historico=historico, resultados=[])

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
    session.clear()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
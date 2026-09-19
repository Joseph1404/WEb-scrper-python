from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__, static_folder="templates/styles", static_url_path="/static")

sessao = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
sessao.mount("http://", HTTPAdapter(max_retries=retry))
sessao.mount("https://", HTTPAdapter(max_retries=retry))


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
    )

if __name__ == "__main__":
    app.run(debug=True)
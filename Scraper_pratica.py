from pathlib import Path

import requests
from bs4 import BeautifulSoup
import csv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# URL do site
url = "https://books.toscrape.com/"

def criar_sessao():
    sessao = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    sessao.mount("https://", HTTPAdapter(max_retries=retry))
    return sessao


def extrair_titulo(livro):
    titulo_elem = livro.find(["h2", "h3"])
    if not titulo_elem:
        return "Título desconhecido"

    link = titulo_elem.find("a")
    if link:
        titulo = link.get("title", "").strip()
        if titulo:
            return titulo

        titulo = link.get_text(strip=True)
        if titulo:
            return titulo

    titulo = titulo_elem.get_text(strip=True)
    return titulo if titulo else "Título desconhecido"


def extrair_preco(livro):
    preco_elem = livro.find("p", class_="price_color")
    if preco_elem:
        return preco_elem.get_text(strip=True)
    return "Preço desconhecido"


def extrair_classificacao(livro):
    classificacao_elem = livro.find("p", class_="star-rating")
    if not classificacao_elem:
        return "Classificação desconhecida"

    classes = classificacao_elem.get("class", [])
    for classe in classes:
        if classe.lower() in {"one", "two", "three", "four", "five"}:
            return classe.capitalize()
    return "Classificação desconhecida"


def extrair_livros(url):
    print("Conectando no site...")
    sessao = criar_sessao()

    try:
        resposta = sessao.get(
            url,
            headers={"User-Agent": "ScraperPratica/1.0"},
            timeout=15,
        )
        resposta.raise_for_status()
    except requests.RequestException as erro:
        print(f"Não foi possível acessar o site: {erro}")
        raise SystemExit(1)

    print("Extraindo dados...")
    soup = BeautifulSoup(resposta.text, "html.parser")
    livros = soup.find_all("article", class_="product_pod")

    dados = []
    for livro in livros:
        titulo = extrair_titulo(livro)
        preco = extrair_preco(livro)
        classificacao = extrair_classificacao(livro)

        dados.append([titulo, preco, classificacao])

    return dados


def salvar_csv(dados, nome_arquivo="livros_pratica.csv"):
    caminho = Path(nome_arquivo)

    with caminho.open("w", newline="", encoding="utf-8") as arquivo:
        writer = csv.writer(arquivo)
        writer.writerow(["Título", "Preço", "Classificação"])
        writer.writerows(dados)

    print(f"Arquivo salvo em: {caminho.resolve()}")


def main():
    url = "https://books.toscrape.com/"
    dados = extrair_livros(url)

    if dados:
        salvar_csv(dados)
        print(f"✅ PRONTO! {len(dados)} livros extraídos!")
    else:
        print("Nenhum livro foi encontrado.")


if __name__ == "__main__":
    main()
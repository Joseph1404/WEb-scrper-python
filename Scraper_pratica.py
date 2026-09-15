import requests
from bs4 import BeautifulSoup
import csv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# URL do site
url = "https://books.toscrape.com/"

print("Conectando no site...")
sessao = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
sessao.mount("https://", HTTPAdapter(max_retries=retry))

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
soup = BeautifulSoup(resposta.text, 'html.parser')

# Encontrar os livros
livros = soup.find_all('article', class_='product_pod')

dados = []

for livro in livros:
    titulo_elem = livro.find(['h2', 'h3'])
    if titulo_elem:
        link_titulo = titulo_elem.find('a')
        titulo = ''
        if link_titulo:
            titulo = link_titulo.get('title', '').strip()
            if not titulo:
                titulo = link_titulo.get_text(strip=True)
        if not titulo:
            titulo = titulo_elem.get_text(strip=True)
        if not titulo:
            titulo = 'Título desconhecido'
    else:
        titulo = 'Título desconhecido'

    preco_elem = livro.find('p', class_='price_color')
    preco = preco_elem.text if preco_elem else 'Preço desconhecido'

    classificacao_elem = livro.find('p', class_='star-rating')
    classes = classificacao_elem.get('class', []) if classificacao_elem else []
    classificacao = classes[1] if len(classes) > 1 else 'Classificação desconhecida'

    dados.append([titulo, preco, classificacao])

# Salvar em CSV
print(f"Salvando {len(dados)} livros_pratica...")
with open('livros_pratica.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['Título', 'Preço', 'Classificação'])
    writer.writerows(dados)

print(f"✅ PRONTO! {len(dados)} livros extraídos!")
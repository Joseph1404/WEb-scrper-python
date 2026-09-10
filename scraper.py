import requests
from bs4 import BeautifulSoup
import csv

# URL do site
url = "https://books.toscrape.com/"

print("Conectando no site...")
resposta = requests.get(url)

print("Extraindo dados...")
soup = BeautifulSoup(resposta.content, 'html.parser')

# Encontrar os livros
livros = soup.find_all('article', class_='product_pod')

dados = []
for livro in livros:
    try:
        # Extrair título de forma mais segura
        titulo_elem = livro.find('h2')
        if titulo_elem:
            titulo = titulo_elem.a.get('title', 'Título desconhecido')
        else:
            titulo = 'Título desconhecido'
        
        # Extrair preço
        preco_elem = livro.find('p', class_='price_color')
        preco = preco_elem.text if preco_elem else 'Preço desconhecido'
        
        dados.append([titulo, preco])
    except:
        pass

# Salvar em CSV
print(f"Salvando {len(dados)} livros...")
with open('livros.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['Título', 'Preço'])
    writer.writerows(dados)

print(f"✅ PRONTO! {len(dados)} livros extraídos!")
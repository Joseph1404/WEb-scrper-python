# Deploy do WebHarbor

## Pré-requisitos

- Servidor Linux atualizado, Docker e Docker Compose instalados.
- Um domínio apontado ao servidor e um proxy reverso com HTTPS (Caddy, Nginx ou serviço equivalente).
- Backup recorrente do diretório `data/`, que contém o SQLite.

## Instalação

1. Copie `.env.example` para `.env` e preencha `WEBHARBOR_SECRET_KEY` com um segredo aleatório forte. Não versione esse arquivo.
2. Crie o diretório persistente: `mkdir -p data`.
3. Suba a aplicação: `docker compose up -d --build`.
4. Configure o proxy reverso para encaminhar o domínio HTTPS para `http://127.0.0.1:8000`.
5. Confira os logs com `docker compose logs -f webharbor`.

O Compose publica a aplicação somente em `127.0.0.1`; não exponha a porta 8000 diretamente à internet. O proxy deve forçar HTTPS e encaminhar `Host`, `X-Forwarded-For` e `X-Forwarded-Proto`.

Por padrão, `WEBHARBOR_PUBLIC_SIGNUP=false`: novos acessos devem ser criados pelo painel administrativo após a compra. Para abrir cadastro público no futuro, altere essa variável para `true` e reinicie o contêiner.

Defina `WEBHARBOR_PUBLIC_URL` com a URL HTTPS pública do sistema. Ela é usada na mensagem de entrega copiada para o Fiverr.

## Operação

- Atualização: faça backup de `data/webharbor.sqlite3`, depois execute `docker compose up -d --build`.
- Backup: copie o arquivo SQLite com a aplicação parada ou use o mecanismo de backup consistente do provedor. Teste restaurações periodicamente.
- Nunca registre no Git o `.env`, o banco SQLite ou relatórios exportados.
- A imagem instala Playwright, mas os navegadores podem exigir `playwright install --with-deps chromium` durante a construção, caso o modo JavaScript seja usado no servidor.
- Restrinja a saída de rede do servidor/container: o coletor não deve alcançar IPs privados, metadados de nuvem ou serviços internos. Isso complementa a validação feita pela aplicação contra SSRF e DNS rebinding.

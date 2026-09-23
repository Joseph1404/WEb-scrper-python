# Registro de segurança e LGPD

## Medidas aplicadas

- Senhas armazenadas com hash do Werkzeug; nunca em texto puro.
- Cookies `HttpOnly`, `SameSite=Lax` e `Secure` em produção.
- Token CSRF nos formulários em produção.
- Cabeçalhos contra clickjacking, MIME sniffing e carregamento de conteúdo não autorizado.
- Bloqueio de IPs locais, privados e reservados nas coletas para reduzir SSRF.
- Relatórios vinculados à sessão do usuário autenticado e expirados após 30 minutos.
- Exclusão de conta remove também suas coletas armazenadas.

## Pendências operacionais antes da abertura pública

1. Definir controlador, canal de direitos do titular, retenção de dados/backups e encarregado quando aplicável.
2. Publicar o aviso de privacidade preenchido e os termos de uso.
3. Configurar HTTPS, backups testados, atualização recorrente do servidor e acesso administrativo com senha forte.
4. Criar procedimento de incidente: registro, contenção, avaliação e comunicação quando aplicável.
5. Avaliar fornecedores de hospedagem, e-mail e monitoramento como operadores e formalizar contratos adequados.

Esta lista é uma orientação técnica, não substitui aconselhamento jurídico. As medidas de segurança e a governança devem ser proporcionais ao risco do tratamento, conforme as orientações da ANPD.

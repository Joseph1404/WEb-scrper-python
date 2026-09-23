import os
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


DATABASE_PATH = Path(
    os.environ.get("WEBHARBOR_DATABASE")
    or Path(__file__).with_name("webharbor.sqlite3")
)


def mes_atual():
    """Retorna o ciclo de uso corrente em UTC, no formato AAAA-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def conectar():
    conexao = sqlite3.connect(DATABASE_PATH)
    conexao.row_factory = sqlite3.Row
    return conexao


def inicializar_banco():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(conectar()) as conexao:
        conexao.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                senha_temporaria INTEGER NOT NULL DEFAULT 0,
                plano TEXT NOT NULL DEFAULT 'gratuito',
                limite_coletas INTEGER NOT NULL DEFAULT 5,
                coletas_mes INTEGER NOT NULL DEFAULT 0,
                mes_referencia TEXT NOT NULL,
                criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coletas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                site TEXT NOT NULL,
                modo_coleta TEXT NOT NULL,
                incluir_imagens INTEGER NOT NULL DEFAULT 0,
                quantidade_registros INTEGER NOT NULL DEFAULT 0,
                campos_extraidos TEXT NOT NULL DEFAULT '[]',
                seletor_itens TEXT,
                criado_em TEXT NOT NULL,
                FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
            );
            """
        )
        colunas = {
            coluna[1]
            for coluna in conexao.execute("PRAGMA table_info(usuarios)").fetchall()
        }
        if "senha_temporaria" not in colunas:
            conexao.execute(
                "ALTER TABLE usuarios ADD COLUMN senha_temporaria INTEGER NOT NULL DEFAULT 0"
            )
        if "limite_coletas" not in colunas:
            conexao.execute(
                "ALTER TABLE usuarios ADD COLUMN limite_coletas INTEGER NOT NULL DEFAULT 5"
            )
        if "mes_referencia" not in colunas:
            conexao.execute(
                "ALTER TABLE usuarios ADD COLUMN mes_referencia TEXT NOT NULL DEFAULT ''"
            )
            conexao.execute(
                "UPDATE usuarios SET mes_referencia = ? WHERE mes_referencia = ''",
                (mes_atual(),),
            )
        colunas_coletas = {
            coluna[1]
            for coluna in conexao.execute("PRAGMA table_info(coletas)").fetchall()
        }
        if "campos_extraidos" not in colunas_coletas:
            conexao.execute(
                "ALTER TABLE coletas ADD COLUMN campos_extraidos TEXT NOT NULL DEFAULT '[]'"
            )
        if "seletor_itens" not in colunas_coletas:
            conexao.execute("ALTER TABLE coletas ADD COLUMN seletor_itens TEXT")
        conexao.commit()


def criar_usuario(email, senha, plano="gratuito", senha_temporaria=False, limite_coletas=None):
    email = email.strip().lower()
    limites = {"gratuito": 5, "basico": 100, "profissional": 1000}
    limite = limite_coletas if limite_coletas is not None else limites.get(plano, 5)
    with closing(conectar()) as conexao:
        try:
            cursor = conexao.execute(
                """
                INSERT INTO usuarios
                    (email, senha_hash, senha_temporaria, plano, limite_coletas, mes_referencia, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    generate_password_hash(senha),
                    int(senha_temporaria),
                    plano,
                    limite,
                    mes_atual(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conexao.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Este e-mail já está cadastrado.") from exc
        return cursor.lastrowid


def autenticar_usuario(email, senha):
    with closing(conectar()) as conexao:
        _resetar_uso_mensal(conexao)
        usuario = conexao.execute(
            "SELECT * FROM usuarios WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    if usuario and check_password_hash(usuario["senha_hash"], senha):
        return dict(usuario)
    return None


def excluir_usuario(usuario_id, senha):
    with closing(conectar()) as conexao:
        usuario = conexao.execute(
            "SELECT senha_hash FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if not usuario or not check_password_hash(usuario["senha_hash"], senha):
            return False
        conexao.execute("DELETE FROM coletas WHERE usuario_id = ?", (usuario_id,))
        conexao.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
        conexao.commit()
    return True


def atualizar_senha(usuario_id, senha):
    with closing(conectar()) as conexao:
        conexao.execute(
            "UPDATE usuarios SET senha_hash = ?, senha_temporaria = 0 WHERE id = ?",
            (generate_password_hash(senha), usuario_id),
        )
        conexao.commit()


def atualizar_limite_usuario(usuario_id, limite_coletas, plano=None):
    if limite_coletas < 0:
        raise ValueError("O limite de coletas não pode ser negativo.")
    with closing(conectar()) as conexao:
        if plano is None:
            conexao.execute(
                "UPDATE usuarios SET limite_coletas = ? WHERE id = ?",
                (limite_coletas, usuario_id),
            )
        else:
            conexao.execute(
                "UPDATE usuarios SET limite_coletas = ?, plano = ? WHERE id = ?",
                (limite_coletas, plano, usuario_id),
            )
        conexao.commit()


def buscar_usuario(usuario_id):
    with closing(conectar()) as conexao:
        _resetar_uso_mensal(conexao, usuario_id)
        usuario = conexao.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
    return dict(usuario) if usuario else None


def listar_usuarios():
    with closing(conectar()) as conexao:
        _resetar_uso_mensal(conexao)
        usuarios = conexao.execute(
            "SELECT * FROM usuarios ORDER BY criado_em DESC"
        ).fetchall()
    return [dict(usuario) for usuario in usuarios]


def resumo_administrativo():
    with closing(conectar()) as conexao:
        _resetar_uso_mensal(conexao)
        resumo = conexao.execute(
            """
            SELECT
                COUNT(*) AS total_clientes,
                COALESCE(SUM(coletas_mes), 0) AS coletas_no_mes,
                COALESCE(SUM(limite_coletas), 0) AS limite_total
            FROM usuarios
            """
        ).fetchone()
    return dict(resumo)


def listar_coletas_recentes(limite=50):
    with closing(conectar()) as conexao:
        coletas = conexao.execute(
            """
            SELECT coletas.*, usuarios.email AS usuario_email
            FROM coletas
            LEFT JOIN usuarios ON usuarios.id = coletas.usuario_id
            ORDER BY coletas.criado_em DESC
            LIMIT ?
            """,
            (limite,),
        ).fetchall()
    resultado = []
    for coleta in coletas:
        item = dict(coleta)
        item["campos_extraidos"] = json.loads(item["campos_extraidos"] or "[]")
        resultado.append(item)
    return resultado


def registrar_coleta(usuario_id, dados):
    with closing(conectar()) as conexao:
        _resetar_uso_mensal(conexao, usuario_id)
        consumo_atualizado = conexao.execute(
            """
            UPDATE usuarios
            SET coletas_mes = coletas_mes + 1
            WHERE id = ? AND coletas_mes < limite_coletas
            """,
            (usuario_id,),
        )
        if consumo_atualizado.rowcount != 1:
            raise ValueError("Seu limite mensal de coletas foi atingido.")
        conexao.execute(
            """
            INSERT INTO coletas
                (usuario_id, site, modo_coleta, incluir_imagens, quantidade_registros,
                 campos_extraidos, seletor_itens, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usuario_id,
                dados["site"],
                dados.get("modo_coleta", "estatica"),
                int(dados.get("incluir_imagens", False)),
                len(dados.get("resultados", [])),
                json.dumps(sorted({campo for item in dados.get("resultados", []) for campo in item})),
                dados.get("seletor_itens"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conexao.commit()


def pode_coletar(usuario):
    if not usuario:
        return True
    if usuario.get("mes_referencia") != mes_atual():
        return True
    return usuario["coletas_mes"] < usuario.get("limite_coletas", 5)


def _resetar_uso_mensal(conexao, usuario_id=None):
    """Zera o contador somente quando o usuário entra em um novo ciclo mensal."""
    ciclo = mes_atual()
    if usuario_id is None:
        conexao.execute(
            """
            UPDATE usuarios
            SET coletas_mes = 0, mes_referencia = ?
            WHERE mes_referencia != ?
            """,
            (ciclo, ciclo),
        )
    else:
        conexao.execute(
            """
            UPDATE usuarios
            SET coletas_mes = 0, mes_referencia = ?
            WHERE id = ? AND mes_referencia != ?
            """,
            (ciclo, usuario_id, ciclo),
        )
    conexao.commit()

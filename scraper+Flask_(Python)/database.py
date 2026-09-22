import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


DATABASE_PATH = Path(
    os.environ.get("WEBHARBOR_DATABASE")
    or Path(__file__).with_name("webharbor.sqlite3")
)


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
                criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coletas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                site TEXT NOT NULL,
                modo_coleta TEXT NOT NULL,
                incluir_imagens INTEGER NOT NULL DEFAULT 0,
                quantidade_registros INTEGER NOT NULL DEFAULT 0,
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
                    (email, senha_hash, senha_temporaria, plano, limite_coletas, criado_em)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    generate_password_hash(senha),
                    int(senha_temporaria),
                    plano,
                    limite,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conexao.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Este e-mail já está cadastrado.") from exc
        return cursor.lastrowid


def autenticar_usuario(email, senha):
    with closing(conectar()) as conexao:
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
        usuario = conexao.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
    return dict(usuario) if usuario else None


def listar_usuarios():
    with closing(conectar()) as conexao:
        usuarios = conexao.execute(
            "SELECT * FROM usuarios ORDER BY criado_em DESC"
        ).fetchall()
    return [dict(usuario) for usuario in usuarios]


def registrar_coleta(usuario_id, dados):
    with closing(conectar()) as conexao:
        conexao.execute(
            """
            INSERT INTO coletas
                (usuario_id, site, modo_coleta, incluir_imagens, quantidade_registros, criado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                usuario_id,
                dados["site"],
                dados.get("modo_coleta", "estatica"),
                int(dados.get("incluir_imagens", False)),
                len(dados.get("resultados", [])),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conexao.execute(
            "UPDATE usuarios SET coletas_mes = coletas_mes + 1 WHERE id = ?",
            (usuario_id,),
        )
        conexao.commit()


def pode_coletar(usuario):
    if not usuario:
        return True
    return usuario["coletas_mes"] < usuario.get("limite_coletas", 5)

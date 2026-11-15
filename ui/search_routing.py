"""Roteamento da pesquisa global: texto -> chave de página. Não importa Qt.

Regras:
* a comparação ignora acentos, emoji, maiúsculas e pontuação;
* a consulta precisa de 2 caracteres; abaixo de 3 só conta uma correspondência
  exata (para "ia" continuar a abrir a página de IA);
* exato (3) > prefixo (2) > palavra inteira (1);
* em caso de empate vence a rota que aparece primeiro em `all_navigation_items()`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Mapping

from ui.navigation import all_navigation_items

MIN_EXACT_LENGTH: Final[int] = 2
MIN_QUERY_LENGTH: Final[int] = 3

EXACT: Final[int] = 3
PREFIX: Final[int] = 2
WORD: Final[int] = 1

# Palavras que os utilizadores escrevem e que não aparecem nos rótulos.
SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "dashboard": ("inicio", "resumo", "visao geral", "home"),
    "panorama": ("panorama financeiro",),
    "centro": ("centro financeiro", "contas"),
    "movimentos": ("transacoes", "lancamentos", "despesas", "receitas"),
    "orcamentos": ("budget", "orcamento"),
    "subscricoes": ("subscricao", "recorrentes"),
    "pagamentos": ("pagamentos futuros", "agendados"),
    "importacao": ("importar", "extrato", "ofx", "csv"),
    "divisao": ("dividir", "partilhar", "splits"),
    "documentos": ("ficheiros", "anexos", "recibos"),
    "carteira": (
        "investimentos",
        "investimento",
        "portfolio",
        "etf",
        "fire",
        "independencia financeira",
    ),
    "objetivos": ("goals", "poupanca para objetivos"),
    "planeador": ("planeamento", "compras planeadas"),
    "metas_mensais": ("metas do mes",),
    "analises": ("relatorios", "analise", "tendencias"),
    "dinamica": ("analise dinamica",),
    "revisao": ("revisao mensal",),
    "historico": ("historico mensal",),
    "relatorios_pdf": ("pdf", "exportar", "exportacao"),
    "saude": ("saude financeira", "indicadores"),
    "score": ("score financeiro",),
    "risco": ("resiliencia", "stress"),
    "simulacoes": ("habitos", "casino", "apostas"),
    "simulador": ("simulador de decisoes", "decisoes"),
    "juros": ("juros compostos", "interesse composto"),
    "assistente": ("assistente local",),
    "ia": ("gemini", "chat", "inteligencia artificial"),
    "definicoes": ("settings", "configuracao", "preferencias"),
    "gestao_dados": ("backup", "seguranca dos dados"),
}


def fold(text: str) -> str:
    """Remove acentos e maiúsculas ('Orçamentos' -> 'orcamentos')."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def simplify(text: str) -> str:
    """`fold` + remove emoji/pontuação e colapsa espaços."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", fold(text)).split())


def _build_route_terms() -> dict[str, tuple[str, ...]]:
    terms: dict[str, list[str]] = {}
    for item in all_navigation_items():
        bucket = terms.setdefault(item.key, [])
        candidates = (
            item.label,
            item.compact_label,
            item.key.replace("_", " "),
            *SYNONYMS.get(item.key, ()),
        )
        for candidate in candidates:
            value = simplify(candidate)
            if value and value not in bucket:
                bucket.append(value)
    return {key: tuple(values) for key, values in terms.items()}


#: Chave de rota -> termos pesquisáveis, pela ordem da navegação.
ROUTE_TERMS: Final[Mapping[str, tuple[str, ...]]] = _build_route_terms()


def _score(query: str, term: str) -> int:
    if query == term:
        return EXACT
    if len(query) < MIN_QUERY_LENGTH:
        return 0
    if term.startswith(query):
        return PREFIX
    if re.search(rf"(?:^| ){re.escape(query)}(?: |$)", term):
        return WORD
    return 0


def resolve_route(text: str) -> str | None:
    """Devolve a chave da página a abrir, ou `None` para pesquisar movimentos."""
    query = simplify(text)
    if len(query) < MIN_EXACT_LENGTH:
        return None
    best_key: str | None = None
    best_score = 0
    for key, terms in ROUTE_TERMS.items():
        for term in terms:
            score = _score(query, term)
            if score > best_score:
                best_key, best_score = key, score
    return best_key

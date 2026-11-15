"""Declarative navigation and related-page workspace registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavigationItem:
    key: str
    label: str
    compact_label: str = ""


@dataclass(frozen=True, slots=True)
class NavigationGroup:
    name: str
    items: tuple[NavigationItem, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """A compact family of pages exposed through one sidebar destination."""

    key: str
    title: str
    primary_key: str
    items: tuple[NavigationItem, ...]

    @property
    def page_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)


PANORAMA_WORKSPACE = WorkspaceSpec(
    key="panorama_center",
    title="🧭 Panorama financeiro",
    primary_key="panorama",
    items=(
        NavigationItem("panorama", "🧭  Panorama geral", "Panorama"),
        NavigationItem("centro", "🌐  Centro financeiro", "Centro"),
    ),
)

MOVEMENTS_WORKSPACE = WorkspaceSpec(
    key="movements_operations",
    title="🔄 Movimentos e operações",
    primary_key="movimentos",
    items=(
        NavigationItem("movimentos", "🔄  Extrato", "Extrato"),
        NavigationItem("importacao", "📥  Importação inteligente", "Importação"),
        NavigationItem("divisao", "🧮  Divisão inteligente", "Divisão"),
        NavigationItem("documentos", "🧾  Documentos", "Documentos"),
    ),
)

BUDGETS_WORKSPACE = WorkspaceSpec(
    key="budgets_goals",
    title="🎯 Orçamentos e objetivos",
    primary_key="orcamentos",
    items=(
        NavigationItem("orcamentos", "🎯  Orçamentos", "Orçamentos"),
        NavigationItem("objetivos", "🏆  Objetivos", "Objetivos"),
    ),
)

PLANNER_WORKSPACE = WorkspaceSpec(
    key="planner_targets",
    title="🗓 Planeador financeiro",
    primary_key="planeador",
    items=(
        NavigationItem("planeador", "🗓  Planeador", "Planeador"),
        NavigationItem("metas_mensais", "📆  Metas mensais", "Metas"),
    ),
)

ANALYSIS_WORKSPACE = WorkspaceSpec(
    key="analysis_reports",
    title="📊 Análise e relatórios",
    primary_key="analises",
    items=(
        NavigationItem("analises", "📊  Visão geral", "Geral"),
        NavigationItem("dinamica", "🧩  Análise dinâmica", "Dinâmica"),
        NavigationItem("revisao", "✅  Revisão mensal", "Revisão"),
        NavigationItem("historico", "🕘  Histórico mensal", "Histórico"),
        NavigationItem("relatorios_pdf", "📄  Relatórios e exportação", "PDF"),
    ),
)

HEALTH_WORKSPACE = WorkspaceSpec(
    key="financial_health",
    title="💚 Saúde financeira",
    primary_key="saude",
    items=(
        NavigationItem("saude", "💚  Visão geral", "Visão"),
        NavigationItem("score", "🩺  Score Financeiro 2.0", "Score"),
        NavigationItem("risco", "🛡️  Risco e resiliência", "Risco"),
    ),
)

SIMULATIONS_WORKSPACE = WorkspaceSpec(
    key="simulations",
    title="🧪 Simulações",
    primary_key="simulacoes",
    items=(
        NavigationItem("simulacoes", "🧪  Hábitos e despesas", "Hábitos"),
        NavigationItem("simulador", "🧠  Simulador de decisões", "Decisões"),
        NavigationItem("juros", "📈  Juros compostos", "Juros"),
    ),
)

ASSISTANT_WORKSPACE = WorkspaceSpec(
    key="assistant_ai",
    title="💡 Assistente e IA",
    primary_key="assistente",
    items=(
        NavigationItem("assistente", "💡  Assistente local", "Local"),
        NavigationItem("ia", "✨  IA Gemini", "Gemini"),
    ),
)

WORKSPACES = (
    PANORAMA_WORKSPACE,
    MOVEMENTS_WORKSPACE,
    BUDGETS_WORKSPACE,
    PLANNER_WORKSPACE,
    SIMULATIONS_WORKSPACE,
    ANALYSIS_WORKSPACE,
    HEALTH_WORKSPACE,
    ASSISTANT_WORKSPACE,
)

_WORKSPACE_BY_PAGE = {
    page_key: workspace for workspace in WORKSPACES for page_key in workspace.page_keys
}


NAVIGATION_GROUPS = (
    NavigationGroup(
        "Início",
        (
            NavigationItem("dashboard", "🏠  Dashboard", "Dashboard"),
            NavigationItem("panorama", "🧭  Panorama financeiro", "Panorama"),
        ),
    ),
    NavigationGroup(
        "Dinheiro & Movimentos",
        (
            NavigationItem("movimentos", "🔄  Movimentos", "Movimentos"),
            NavigationItem("subscricoes", "🔁  Subscrições", "Subscrições"),
            NavigationItem("pagamentos", "⏳  Pagamentos futuros", "Pagamentos"),
        ),
    ),
    NavigationGroup(
        "Planear & Crescer",
        (
            NavigationItem("orcamentos", "🎯  Orçamentos", "Orçamentos"),
            NavigationItem("planeador", "🗓  Planeador", "Planeador"),
            NavigationItem("carteira", "💼  Investimentos", "Carteira"),
            NavigationItem("simulacoes", "🧪  Simulações", "Simulações"),
        ),
    ),
    NavigationGroup(
        "Analisar & Auditar",
        (
            NavigationItem("analises", "📊  Análise e relatórios", "Análises"),
            NavigationItem("saude", "💚  Saúde financeira", "Saúde"),
        ),
    ),
    NavigationGroup(
        "Sistema & Inteligência",
        (
            NavigationItem("assistente", "💡  Assistente e IA", "Assistente"),
            NavigationItem("gestao_dados", "🧬  Gestão dos dados", "Dados"),
            NavigationItem("definicoes", "⚙  Definições", "Definições"),
        ),
    ),
)


def workspace_for_page(page_key: str) -> WorkspaceSpec | None:
    return _WORKSPACE_BY_PAGE.get(str(page_key or ""))


def navigation_key_for_page(page_key: str) -> str:
    workspace = workspace_for_page(page_key)
    return workspace.primary_key if workspace is not None else str(page_key or "")


def all_navigation_items() -> tuple[NavigationItem, ...]:
    """Return every route, including pages folded into a workspace."""

    ordered: list[NavigationItem] = []
    seen: set[str] = set()
    for group in NAVIGATION_GROUPS:
        for item in group.items:
            if item.key not in seen:
                ordered.append(item)
                seen.add(item.key)
            workspace = workspace_for_page(item.key)
            if workspace is None:
                continue
            for child in workspace.items:
                if child.key not in seen:
                    ordered.append(child)
                    seen.add(child.key)
    return tuple(ordered)


def navigation_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    """Return the tuple shape expected by the current sidebar builder."""

    return [
        (group.name, [(item.key, item.label) for item in group.items])
        for group in NAVIGATION_GROUPS
    ]


def contextual_navigation_label(page_key: str) -> str:
    """Return an icon plus a short label that fits the 960 px sidebar."""

    key = str(page_key or "")
    item = next(
        (
            candidate
            for group in NAVIGATION_GROUPS
            for candidate in group.items
            if candidate.key == key
        ),
        None,
    )
    if item is None:
        item = next(
            (
                candidate
                for ws in WORKSPACES
                for candidate in ws.items
                if candidate.key == key
            ),
            None,
        )
    if item is None:
        return ""
    icon = item.label.split("  ", 1)[0]
    text = item.compact_label or item.label.replace("  ", " ").strip()
    return f"{icon} {text}".strip()

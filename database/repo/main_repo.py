#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

from .ai import AiRepoMixin
from .analytics import AnalyticsRepoMixin
from .base import BaseRepo
from .documents import DocumentsRepoMixin
from .identity import IdentityRepoMixin
from .planning import PlanningRepoMixin
from .product_review import ProductReviewRepoMixin
from .reimbursements import ReimbursementsRepoMixin
from .subscription_radar import SubscriptionRadarRepoMixin
from .simulations import SimulationsRepoMixin
from .transactions import TransactionsRepoMixin


class Repo(
    ProductReviewRepoMixin,
    SubscriptionRadarRepoMixin,
    ReimbursementsRepoMixin,
    IdentityRepoMixin,
    TransactionsRepoMixin,
    SimulationsRepoMixin,
    PlanningRepoMixin,
    AnalyticsRepoMixin,
    AiRepoMixin,
    DocumentsRepoMixin,
    BaseRepo,
):
    pass

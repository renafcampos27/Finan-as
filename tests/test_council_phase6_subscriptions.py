"""I1 subscription charge proposals remain evidence until explicitly reviewed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.subscription_radar import SubscriptionRadarService


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "radar.sqlite3"))
    yield instance
    instance.db.conn.close()


def test_radar_review_contract_is_available(repo, tmp_path):
    if os.environ.get("FINANCAS_PESSOAIS_PHASE6_BEFORE") == "1":
        before = (
            Path(__file__).resolve().parents[1]
            / "artifacts/council_2026-10-31_phase6/before"
        )
        script = (
            "import json,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(before)!r})\n"
            "from database.engine import FinanceDB\nfrom database.repo import Repo\n"
            f"db=FinanceDB(Path({str(tmp_path / 'before.sqlite3')!r}))\n"
            "r=Repo(db)\n"
            "print(json.dumps({'api':callable(getattr(r,'subscription_price_reviews',None)),"
            "'table':bool(db.conn.execute(\"SELECT 1 FROM sqlite_master WHERE name='subscription_price_reviews'\").fetchone())}))\n"
            "db.conn.close()\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
        capabilities = json.loads(completed.stdout.splitlines()[-1])
    else:
        capabilities = {
            "api": callable(getattr(repo, "subscription_price_reviews", None)),
            "table": bool(
                repo.q(
                    "SELECT 1 FROM sqlite_master WHERE name='subscription_price_reviews'"
                )
            ),
        }
    assert capabilities == {"api": True, "table": True}


def test_radar_without_active_subscriptions_does_not_read_the_ledger(repo, monkeypatch):
    assert repo.subscriptions(active_only=True) == []

    def forbidden(*args):
        pytest.fail("An empty radar scanned the transaction ledger")

    monkeypatch.setattr(repo, "subscription_radar_transactions", forbidden)
    assert SubscriptionRadarService(repo).pending() == []


def subscription(repo, *, name="Media", amount=10, period="Mensal", aliases=""):
    identity = repo.add_subscription(
        name, amount, "Diversos", 1, period, account="Conta principal", aliases=aliases
    )
    repo.commit()
    return identity


def charge(repo, amount=15, *, description="Media", day="2026-02-01"):
    identity = repo.add_transaction(
        day, description, -amount, "Diversos", "Conta principal"
    )
    repo.commit()
    return identity


def accept(service, candidate, **overrides):
    options = dict(
        status="accepted",
        classification="price",
        confirmed_period=candidate.expected_period,
        confirmed_currency="EUR",
        match_confirmed=True,
    )
    options.update(overrides)
    return service.decide(candidate.evidence_key, **options)


def test_discovery_is_read_only_and_does_not_change_price_or_money(repo):
    sid = subscription(repo)
    tx = charge(repo)
    before = repo.db.conn.total_changes
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    assert repo.db.conn.total_changes == before
    assert not repo.db.conn.in_transaction
    assert repo.subscription_price_reviews() == []
    assert candidate.classification == "unclassified" and candidate.status == "pending"
    assert (candidate.subscription_id, candidate.transaction_id) == (sid, tx)
    assert json.loads(candidate.evidence_json)["original_charge_currency"] == "unknown"
    assert repo.subscription_by_id(sid)["amount"] == 10
    assert repo.movement_count() == 1


def test_single_annual_charge_is_reviewable_against_configured_annual_baseline(repo):
    subscription(repo, amount=100, period="Anual")
    charge(repo, 120)
    candidate = SubscriptionRadarService(repo).discover()[0]
    assert candidate.expected_period == "Anual"
    assert (candidate.expected_amount, candidate.observed_amount) == (100, 120)
    assert candidate.increase_pct == pytest.approx(20)


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed_period", "Anual"),
        ("confirmed_currency", "USD"),
        ("confirmed_currency", ""),
        ("match_confirmed", False),
    ],
)
def test_price_acceptance_requires_equivalent_confirmed_basis(repo, field, value):
    subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    with pytest.raises(ValueError, match="confirma"):
        accept(service, service.discover()[0], **{field: value})
    assert repo.subscription_price_reviews() == []


def test_acceptance_records_only_decision_and_survives_read_only_rediscovery(repo):
    sid = subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    review = accept(service, candidate)
    assert review > 0
    assert repo.subscription_by_id(sid)["amount"] == 10
    assert repo.movement_count() == 1
    assert repo.scalar("SELECT SUM(amount) FROM transactions") == -15
    assert service.pending() == []
    observed = service.discover()[0]
    assert observed.evidence_key == candidate.evidence_key
    assert observed.status == "accepted" and observed.classification == "price"
    assert service.history()[0]["stale"] is False


@pytest.mark.parametrize("classification", ["plan", "matching"])
def test_other_classifications_require_explanation_and_do_not_reprice(
    repo, classification
):
    sid = subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    with pytest.raises(ValueError, match="Explica"):
        accept(service, candidate, classification=classification, note="")
    accept(
        service,
        candidate,
        classification=classification,
        confirmed_period="Anual",
        confirmed_currency="USD",
        match_confirmed=False,
        note="Plano e base diferentes",
    )
    stored = repo.subscription_price_reviews()[0]
    assert stored["classification"] == classification
    assert (
        stored["confirmed_period"] == "Anual" and stored["confirmed_currency"] == "USD"
    )
    assert repo.subscription_by_id(sid)["amount"] == 10


def test_explicit_pending_rejected_and_accepted_states_are_stable(repo):
    subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    identity = service.decide(candidate.evidence_key, status="pending")
    candidate = service.discover()[0]
    assert (
        service.decide(candidate.evidence_key, status="rejected", note="Não é aumento")
        == identity
    )
    assert service.pending() == []
    candidate = service.discover()[0]
    assert accept(service, candidate) == identity
    assert len(repo.subscription_price_reviews()) == 1


def test_exact_confirmed_alias_and_occurrence_are_explained(repo):
    sid = subscription(repo, aliases="MEDIA BILLING")
    tx = charge(repo, description="media billing")
    service = SubscriptionRadarService(repo)
    assert service.discover()[0].match_quality == "exact"
    repo.add_subscription_occurrence(sid, "2026-02-01", tx)
    candidate = service.discover()[0]
    assert candidate.match_quality == "occurrence"
    assert "ocorrência" in candidate.reason


def test_ambiguous_and_approximate_matches_never_become_price_facts(repo):
    subscription(repo, name="Media", aliases="Shared billing")
    subscription(repo, name="Other media", aliases="Shared billing")
    charge(repo, description="Shared billing")
    service = SubscriptionRadarService(repo)
    candidates = service.discover()
    assert len(candidates) == 2
    assert all(
        item.match_quality == "ambiguous" and item.classification == "unclassified"
        for item in candidates
    )
    charge(repo, description="Media premium extra", day="2026-03-01")
    assert any(item.match_quality == "possible" for item in service.discover())
    assert repo.subscription_price_reviews() == []


def test_latest_nonincrease_suppresses_older_large_charge_and_future_is_excluded(repo):
    subscription(repo)
    charge(repo, amount=30)
    charge(repo, amount=10, day="2026-03-01")
    charge(repo, amount=60, day="2099-01-01")
    assert SubscriptionRadarService(repo).discover() == []


def test_payment_date_is_the_financial_cutoff(repo):
    subscription(repo)
    tx = charge(repo)
    repo.set_transaction_meta_ext(tx, payment_date="2099-01-01")
    assert SubscriptionRadarService(repo).discover() == []


def test_mutation_after_preview_and_explicit_same_id_replacement_are_refused(repo):
    subscription(repo)
    tx = charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    original = dict(repo.q("SELECT * FROM transactions WHERE id=?", (tx,))[0])
    repo.db.conn.execute("DELETE FROM transactions WHERE id=?", (tx,))
    columns = list(original)
    repo.db.conn.execute(
        "INSERT INTO transactions("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(original.values()),
    )
    repo.db.conn.commit()
    with pytest.raises(ValueError, match="origem mudou"):
        accept(service, candidate)
    assert repo.subscription_price_reviews() == []


def test_deleted_persisted_source_is_never_rebound_to_reused_id(repo):
    sid = subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    service.decide(candidate.evidence_key, status="pending")
    original = dict(repo.subscription_by_id(sid))
    repo.delete_subscription(sid)
    columns = list(original)
    repo.db.conn.execute(
        "INSERT INTO subscriptions("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(original.values()),
    )
    repo.db.conn.commit()
    assert service.discover() == []
    history = service.history()[0]
    assert history["subscription_id"] is None and history["stale"]


def test_caller_owned_transaction_and_rollback_are_preserved(repo):
    subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('outer-marker','pending')"
    )
    candidate = service.discover()[0]
    accept(service, candidate)
    assert repo.db.conn.in_transaction
    assert len(repo.subscription_price_reviews()) == 1
    repo.db.conn.rollback()
    assert repo.subscription_price_reviews() == []
    assert repo.setting("outer-marker", "") == ""


def test_external_connection_change_invalidates_preview(repo):
    import sqlite3

    subscription(repo)
    tx = charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    with sqlite3.connect(repo.db.path) as other:
        other.execute("UPDATE transactions SET amount=-16 WHERE id=?", (tx,))
    with pytest.raises(ValueError, match="origem mudou"):
        accept(service, candidate)
    assert service.discover()[0].observed_amount == 16


def test_one_charge_cannot_be_accepted_as_price_for_two_subscriptions(repo):
    subscription(repo, name="One", aliases="Shared")
    subscription(repo, name="Two", aliases="Shared")
    charge(repo, description="Shared")
    service = SubscriptionRadarService(repo)
    first, second = service.discover()
    accept(service, first)
    candidates = service.discover()
    second = next(
        item for item in candidates if item.subscription_id == second.subscription_id
    )
    with pytest.raises(ValueError, match="outra subscrição"):
        accept(service, second)
    assert repo.movement_count() == 1


def test_review_persists_across_protected_reopen_without_changing_ledger(repo):
    sid = subscription(repo)
    charge(repo)
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    accept(service, candidate)
    repo.db.conn.close()
    repo.db = FinanceDB(repo.db.path)
    stored = SubscriptionRadarService(repo).discover()[0]
    assert stored.status == "accepted" and stored.evidence_key == candidate.evidence_key
    assert repo.subscription_by_id(sid)["amount"] == 10
    assert repo.movement_count() == 1


def test_failed_review_write_rolls_back_only_its_savepoint(repo):
    import sqlite3

    subscription(repo)
    charge(repo)
    repo.db.conn.execute(
        "CREATE TRIGGER test_radar_failure BEFORE INSERT ON subscription_price_reviews "
        "BEGIN SELECT RAISE(ABORT,'synthetic review failure'); END"
    )
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('outer-fault','pending')"
    )
    service = SubscriptionRadarService(repo)
    candidate = service.discover()[0]
    with pytest.raises(sqlite3.IntegrityError, match="synthetic review failure"):
        accept(service, candidate)
    assert repo.db.conn.in_transaction
    assert repo.setting("outer-fault", "") == "pending"
    assert repo.subscription_price_reviews() == [] and repo.movement_count() == 1
    repo.db.conn.rollback()
    repo.db.conn.execute("DROP TRIGGER test_radar_failure")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp, repo):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget
    from ui.core.theme import Theme
    from ui.subscription_radar import SubscriptionRadarDialog

    subscription(repo, name="<b>Private media</b>")
    charge(repo, description="<b>Private media</b>")
    parent = QWidget()
    parent.theme = Theme()
    window = SubscriptionRadarDialog(repo, parent)
    window.show()
    qapp.processEvents()
    yield window
    for owned in (window, parent):
        owned.close()
        owned.deleteLater()
        QCoreApplication.sendPostedEvents(owned, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_radar_dialog_preserves_literal_text_and_requires_confirmation(dialog, repo):
    from PySide6.QtCore import Qt

    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == "<b>Private media</b>"
    assert dialog.detail.textFormat() == Qt.PlainText
    assert "<b>Private media</b>" in dialog.detail.text()
    assert not dialog._decide("accepted")
    assert repo.subscription_price_reviews() == []
    assert "Distingue" in dialog.status.text()


def test_radar_dialog_explicit_acceptance_is_keyboard_reachable(dialog, repo, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    dialog.classification.setCurrentIndex(dialog.classification.findData("price"))
    dialog.period.setCurrentIndex(dialog.period.findData("Mensal"))
    dialog.currency.setText("EUR")
    dialog.match.setChecked(True)
    dialog.classification.setFocus()
    visited = []
    for _ in range(30):
        focused = dialog.focusWidget()
        visited.append(type(focused).__name__)
        if focused is dialog.accept_button:
            break
        QTest.keyClick(focused, Qt.Key_Tab)
    assert dialog.accept_button.hasFocus(), visited
    QTest.keyClick(dialog.accept_button, Qt.Key_Space)
    qapp.processEvents()
    assert repo.subscription_price_reviews()[0]["status"] == "accepted"
    assert repo.movement_count() == 1
    assert dialog.changed


def test_radar_dialog_private_mode_hides_all_detail_and_blocks_writes(dialog, repo):
    dialog.theme.privacy = True
    dialog._check_privacy()
    assert dialog.table.item(0, 0).text() == "Dados ocultos"
    assert "Private media" not in dialog.detail.text()
    assert "15" not in dialog.table.item(0, 3).text()
    assert (
        not dialog.accept_button.isEnabled() and not dialog.pending_button.isEnabled()
    )
    assert not dialog._decide("accepted")
    assert repo.subscription_price_reviews() == []


def test_private_change_masks_retained_review_notes(dialog, repo):
    dialog.note.setPlainText("Private note and amount 15.00")
    assert dialog._decide("rejected")
    assert "Private note" in dialog.history_table.item(0, 4).text()
    dialog.theme.privacy = True
    dialog._check_privacy()
    assert dialog.history_table.item(0, 4).text() == "Dados ocultos"
    assert dialog.note.toPlainText() == ""
    assert repo.subscription_price_reviews()[0]["status"] == "rejected"


def test_radar_error_is_visible_and_preserves_money(dialog, repo, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("synthetic unavailable storage")

    monkeypatch.setattr(dialog.service, "decide", failed)
    assert not dialog._decide("pending")
    assert "Não foi possível guardar" in dialog.status.text()
    assert repo.subscription_price_reviews() == [] and repo.movement_count() == 1


def test_dialog_rejection_stops_owned_privacy_timer(dialog):
    assert dialog._privacy_timer.isActive()
    dialog.reject()
    assert not dialog._privacy_timer.isActive()


def test_replaced_app_theme_masks_before_a_decision_without_timer_delay(dialog, repo):
    from ui.core.theme import Theme

    dialog.parentWidget().theme = Theme(privacy=True)
    assert not dialog._decide("pending")
    assert dialog.table.item(0, 0).text() == "Dados ocultos"
    assert not dialog.accept_button.isEnabled()
    assert repo.subscription_price_reviews() == []


@pytest.mark.parametrize(
    "classification,period,currency,confirmed",
    [
        ("unclassified", "", "", 0),
        ("price", "Mensal", "USD", 1),
        ("price", "Anual", "EUR", 1),
        ("price", "Mensal", "EUR", 0),
        ("price", "", "EUR", 1),
    ],
)
def test_storage_refuses_unconfirmed_or_incomparable_accepted_price(
    repo, classification, period, currency, confirmed
):
    import sqlite3

    subscription(repo)
    charge(repo)
    candidate = SubscriptionRadarService(repo).discover()[0]
    with pytest.raises(sqlite3.IntegrityError):
        repo.db.conn.execute(
            "INSERT INTO subscription_price_reviews("
            "subscription_id,transaction_id,evidence_key,evidence_json,classification,status,"
            "confirmed_period,confirmed_currency,match_confirmed) "
            "VALUES(?,?,?,?,?,'accepted',?,?,?)",
            (
                candidate.subscription_id,
                candidate.transaction_id,
                candidate.evidence_key,
                candidate.evidence_json,
                classification,
                period,
                currency,
                confirmed,
            ),
        )
    repo.db.conn.rollback()
    assert repo.subscription_price_reviews() == []

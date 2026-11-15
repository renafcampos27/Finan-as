#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Empirical benchmark, compression ratio and storage optimization verifier for Milestone 3."""

from __future__ import annotations

import gc
import json
import statistics
import time
import tracemalloc
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.engine import FinanceDB
from database.repo import Repo
from services.backup import BackupService
from services.change_journal import (
    compress_payload,
    decompress_payload,
)


def benchmark_change_journal_payloads() -> dict[str, Any]:
    """Empirically measure compression ratio, compression throughput, and decompression throughput."""
    results = {}

    # 1. Small payload (< 1KB, ~300 bytes)
    small_data = {
        "action": "edit",
        "movement_id": 1042,
        "old_value": {"amount": -45.50, "category": "Alimentação", "notes": "Jantar"},
        "new_value": {"amount": -48.00, "category": "Restaurantes", "notes": "Jantar com sobremesa"},
    }
    small_json = json.dumps(small_data, ensure_ascii=False)

    # 2. Medium payload (~10KB)
    medium_data = {
        "action": "bulk_update",
        "batch_id": "BATCH-2026-09-02-001",
        "description": "Atualização de categorias de despesas correntes de verão",
        "rows": [
            {
                "id": i,
                "date": f"2026-07-{(i % 28) + 1:02d}",
                "amount": -round(12.34 * (i + 1), 2),
                "category": "Supermercado e Mercearia",
                "notes": f"Compra semanal do mês {i} com talão nº {1000 + i} € à taxa de 23%",
                "account": "Conta à Ordem Principal",
            }
            for i in range(50)
        ],
    }
    medium_json = json.dumps(medium_data, ensure_ascii=False)

    # 3. Large payload (~100KB)
    large_data = {
        "action": "import_ofx_reconciliation",
        "source": "Banco Comercial Português - Extrato Consolidado",
        "rows": [
            {
                "id": i,
                "fitid": f"FITID-20260902-{i:05d}-EUR",
                "date": f"2026-08-{(i % 28) + 1:02d}",
                "amount": round((i % 7 - 3) * 153.75, 2),
                "description": f"Movimento financeiro complexo com caracteres especiais: Açores, São Tomé & Príncipe, €uro nº {i}",
                "category": "Despesas Gerais de Habitação & Utilidades",
                "notes": "Auditoria de precisão financeira com taxas de juro calculadas a 3.7542% anuais.",
                "tags": ["essencial", "recorrente", "fiscal_irs_cat_g"],
            }
            for i in range(500)
        ],
    }
    large_json = json.dumps(large_data, ensure_ascii=False)

    # 4. Extra Large payload (~1MB)
    xlarge_data = {
        "action": "full_state_snapshot",
        "rows": [
            {
                "id": i,
                "fitid": f"FULL-SNAP-{i:06d}",
                "date": "2026-09-01",
                "amount": round(-19.99 * (i % 50 + 1), 2),
                "description": f"Registo histórico de movimento com detalhe longo para medição de throughput em MB/s nº {i}",
                "meta": {"tax_lot": i, "holding_pct": 0.0425, "currency": "EUR"},
            }
            for i in range(5000)
        ],
    }
    xlarge_json = json.dumps(xlarge_data, ensure_ascii=False)

    payloads = [
        ("small_300B", small_json),
        ("medium_10KB", medium_json),
        ("large_100KB", large_json),
        ("xlarge_1MB", xlarge_json),
    ]

    for label, raw_json in payloads:
        raw_bytes = raw_json.encode("utf-8")
        raw_size = len(raw_bytes)

        # Warmup
        for _ in range(5):
            c = compress_payload(raw_json)
            d = decompress_payload(c)
            assert d == raw_json

        # Benchmark Compression
        trials = 100 if raw_size < 200_000 else 30
        c_times = []
        for _ in range(trials):
            t0 = time.perf_counter()
            comp = compress_payload(raw_json)
            t1 = time.perf_counter()
            c_times.append(t1 - t0)

        comp_size = len(comp)
        compression_ratio = raw_size / comp_size if comp_size > 0 else 1.0
        reduction_pct = ((raw_size - comp_size) / raw_size) * 100.0 if raw_size > 0 else 0.0

        median_c_sec = statistics.median(c_times)
        c_throughput_mb_s = (raw_size / (1024 * 1024)) / median_c_sec if median_c_sec > 0 else 0.0

        # Benchmark Decompression
        d_times = []
        for _ in range(trials):
            t0 = time.perf_counter()
            decomp = decompress_payload(comp)
            t1 = time.perf_counter()
            d_times.append(t1 - t0)

        median_d_sec = statistics.median(d_times)
        d_throughput_mb_s = (raw_size / (1024 * 1024)) / median_d_sec if median_d_sec > 0 else 0.0

        # Invariant & Precision check
        assert decomp == raw_json
        parsed = json.loads(decomp)
        if "rows" in parsed and len(parsed["rows"]) > 0:
            assert parsed["rows"][0]["amount"] == json.loads(raw_json)["rows"][0]["amount"]

        results[label] = {
            "raw_bytes": raw_size,
            "compressed_bytes": comp_size,
            "prefix": comp[:5].decode("ascii", errors="replace"),
            "compression_ratio": round(compression_ratio, 2),
            "reduction_pct": round(reduction_pct, 2),
            "median_compression_ms": round(median_c_sec * 1000.0, 3),
            "compression_throughput_mb_s": round(c_throughput_mb_s, 2),
            "median_decompression_ms": round(median_d_sec * 1000.0, 3),
            "decompression_throughput_mb_s": round(d_throughput_mb_s, 2),
        }

    return results


def benchmark_backup_compression_and_fidelity(tmp_dir: Path) -> dict[str, Any]:
    """Empirically measure backup compression efficiency, reduction % and restore integrity."""
    db_file = tmp_dir / "benchmark_source.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    attachments_dir = tmp_dir / "anexos"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Seed 1,000 realistic transactions with Portuguese text and float cents
    total_injected_amount = 0.0
    for i in range(1, 1001):
        amt = -round(15.75 + (i * 0.13), 2)
        total_injected_amount += amt
        db.add_transaction(
            f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            f"Compra mensal de supermercado e despesas nº {i}",
            amt,
            "Supermercado",
            "Conta principal",
            "expense",
            notes=f"Nota fiscal portuguesa com NIF 999888777 e valor de € {abs(amt):.2f} " * 6,
            commit=False,
        )
    db.conn.commit()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # Add 5 simulated document attachments
    for doc_id in range(1, 6):
        doc_path = attachments_dir / str(doc_id) / f"comprovativo_{doc_id}.txt"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(f"Documento fiscal de suporte nº {doc_id}\n" + ("Linha de detalhe de auditoria.\n" * 100), encoding="utf-8")
        db.conn.execute(
            """
            INSERT INTO transaction_attachments(transaction_id, file_name, stored_name, stored_path)
            VALUES(?, ?, ?, ?)
            """,
            (doc_id, f"comprovativo_{doc_id}.txt", f"comprovativo_{doc_id}.txt", str(doc_path)),
        )
    db.conn.commit()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    source_db_size = db_file.stat().st_size
    backup_service = BackupService(repo, backup_directory=tmp_dir / "backups")
    backup_file = tmp_dir / "backups" / "backup_benchmark.zip"

    # Profile backup creation time and memory
    tracemalloc.start()
    t0 = time.perf_counter()
    backup_service.export_backup_zip(backup_file)
    t1 = time.perf_counter()
    _, peak_mem_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    backup_size = backup_file.stat().st_size
    duration_ms = (t1 - t0) * 1000.0

    efficiency = BackupService.calculate_backup_efficiency(source_db_size, backup_size)

    # Validate archive manifest and SHA-256
    manifest = BackupService.validate_complete_archive(backup_file)

    # Restore into fresh DB and verify 100% relational integrity and zero float drift
    restored_db_file = tmp_dir / "restored_benchmark.sqlite3"
    t_restore_0 = time.perf_counter()
    BackupService.restore_missing_profile(
        backup_file,
        database_file=restored_db_file,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
    )
    t_restore_1 = time.perf_counter()
    restore_duration_ms = (t_restore_1 - t_restore_0) * 1000.0

    restored_db = FinanceDB(restored_db_file)
    restored_count = int(restored_db.scalar("SELECT COUNT(*) FROM transactions") or 0)
    restored_sum = float(restored_db.scalar("SELECT SUM(amount) FROM transactions") or 0.0)

    db.close()
    repo.close()
    restored_db.close()
    gc.collect()

    assert restored_count == 1000
    assert abs(restored_sum - total_injected_amount) < 1e-4

    return {
        "source_db_bytes": source_db_size,
        "backup_archive_bytes": backup_size,
        "compression_ratio": efficiency["compression_ratio"],
        "reduction_pct": efficiency["reduction_pct"],
        "backup_duration_ms": round(duration_ms, 2),
        "restore_duration_ms": round(restore_duration_ms, 2),
        "peak_memory_kb": round(peak_mem_bytes / 1024.0, 2),
        "sha256_verified": bool(manifest.get("database", {}).get("sha256")),
        "records_verified": restored_count,
        "sum_exact_match": bool(abs(restored_sum - total_injected_amount) < 1e-4),
    }


def benchmark_storage_optimization_and_vacuum(tmp_dir: Path) -> dict[str, Any]:
    """Empirically measure optimize_storage() and incremental_vacuum() execution and memory overhead."""
    db_file = tmp_dir / "vacuum_test.sqlite3"
    db = FinanceDB(db_file)

    # Insert 3,000 transactions to expand database pages
    for i in range(1, 3001):
        db.add_transaction(
            "2026-08-15",
            f"Movimento de teste de armazenamento {i}",
            -20.0,
            "Supermercado",
            "Conta principal",
            "expense",
            notes="Texto longo para inflacionar páginas do SQLite e testar freelist " * 8,
            commit=False,
        )
    db.conn.commit()

    size_before_delete = db_file.stat().st_size

    # Delete 1,500 transactions (50%) to generate freelist pages
    db.conn.execute("DELETE FROM transactions WHERE id % 2 = 0")
    db.conn.commit()

    # Measure incremental_vacuum bounded pages
    t_vac_0 = time.perf_counter()
    vac_bounded = db.incremental_vacuum(pages=20)
    t_vac_1 = time.perf_counter()
    vac_bounded_ms = (t_vac_1 - t_vac_0) * 1000.0

    # Measure optimize_storage() complete run
    tracemalloc.start()
    t_opt_0 = time.perf_counter()
    opt_result = db.optimize_storage()
    t_opt_1 = time.perf_counter()
    _, peak_opt_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    opt_duration_ms = (t_opt_1 - t_opt_0) * 1000.0
    size_after_opt = db_file.stat().st_size

    # Read-only rejection test
    db.close()
    ro_db = FinanceDB.open_read_only(db_file)
    ro_permission_denied = False
    try:
        ro_db.optimize_storage()
    except PermissionError:
        ro_permission_denied = True
    finally:
        ro_db.close()
        gc.collect()

    return {
        "size_before_delete_bytes": size_before_delete,
        "size_after_opt_bytes": size_after_opt,
        "freed_bytes": opt_result.get("freed_bytes", 0),
        "incremental_vacuum_bounded_pages": vac_bounded,
        "incremental_vacuum_bounded_ms": round(vac_bounded_ms, 3),
        "optimize_storage_duration_ms": round(opt_duration_ms, 2),
        "optimize_storage_reported_ms": opt_result.get("duration_ms", 0.0),
        "optimize_storage_peak_memory_kb": round(peak_opt_bytes / 1024.0, 2),
        "checkpoint_status": opt_result.get("checkpoint_status"),
        "integrity_status": opt_result.get("status"),
        "read_only_protection_enforced": ro_permission_denied,
    }


def main():
    import tempfile
    print("=" * 70)
    print("EMPIRICAL BENCHMARK & VERIFICATION HARNESS — MILESTONE 3")
    print("=" * 70)

    # 1. Payload Compression
    print("\n--- 1. CHANGE JOURNAL PAYLOAD COMPRESSION BENCHMARK ---")
    payload_results = benchmark_change_journal_payloads()
    for label, res in payload_results.items():
        print(f"\n[{label}] ({res['raw_bytes']:,} bytes raw):")
        print(f"  Prefix: {res['prefix']} | Comp Size: {res['compressed_bytes']:,} bytes")
        print(f"  Ratio: {res['compression_ratio']}x | Reduction: {res['reduction_pct']}%")
        print(f"  Compression Latency: {res['median_compression_ms']} ms ({res['compression_throughput_mb_s']} MB/s)")
        print(f"  Decompression Latency: {res['median_decompression_ms']} ms ({res['decompression_throughput_mb_s']} MB/s)")

    with tempfile.TemporaryDirectory(prefix="m3_bench_bak_") as temp_bak_dir:
        tmp_bak_path = Path(temp_bak_dir)

        # 2. Backup Compression
        print("\n--- 2. BACKUP COMPRESSION & LOSSLESS FIDELITY BENCHMARK ---")
        backup_results = benchmark_backup_compression_and_fidelity(tmp_bak_path)
        print(f"  Source DB Size: {backup_results['source_db_bytes']:,} bytes")
        print(f"  Backup Archive Size: {backup_results['backup_archive_bytes']:,} bytes")
        print(f"  Compression Ratio: {backup_results['compression_ratio']}x | Reduction: {backup_results['reduction_pct']}%")
        print(f"  Export Duration: {backup_results['backup_duration_ms']} ms | Peak RAM: {backup_results['peak_memory_kb']} KB")
        print(f"  Restore Duration: {backup_results['restore_duration_ms']} ms")
        print(f"  SHA-256 Verified: {backup_results['sha256_verified']} | Sum Exact Match: {backup_results['sum_exact_match']}")

    with tempfile.TemporaryDirectory(prefix="m3_bench_vac_") as temp_vac_dir:
        tmp_vac_path = Path(temp_vac_dir)

        # 3. Storage Optimization
        print("\n--- 3. STORAGE OPTIMIZATION & INCREMENTAL VACUUM BENCHMARK ---")
        storage_results = benchmark_storage_optimization_and_vacuum(tmp_vac_path)
        print(f"  Size Before Delete: {storage_results['size_before_delete_bytes']:,} bytes")
        print(f"  Size After Optimize: {storage_results['size_after_opt_bytes']:,} bytes (Freed: {storage_results['freed_bytes']:,} bytes)")
        print(f"  Incremental Vacuum (20 pages): {storage_results['incremental_vacuum_bounded_ms']} ms")
        print(f"  Optimize Storage Duration: {storage_results['optimize_storage_duration_ms']} ms (Peak RAM: {storage_results['optimize_storage_peak_memory_kb']} KB)")
        print(f"  Checkpoint: {storage_results['checkpoint_status']} | Integrity: {storage_results['integrity_status']}")
        print(f"  Read-Only Protection Enforced: {storage_results['read_only_protection_enforced']}")

    print("\n" + "=" * 70)
    print("ALL EMPIRICAL MEASUREMENTS COMPLETED SUCCESSFULLY.")
    print("=" * 70)

    # Dump JSON results for reporting
    all_metrics = {
        "payload_compression": payload_results,
        "backup_compression": backup_results,
        "storage_optimization": storage_results,
    }
    out_file = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "challenger_m3"
        / "benchmark_results.json"
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(all_metrics, indent=2), encoding="utf-8")
    print(f"\nSaved raw metrics to {out_file}")


if __name__ == "__main__":
    main()

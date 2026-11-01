from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from observatorio import storage
from observatorio.db import connect, sql_path
from observatorio.downloads import atomic_write_text


def test_substituicao_repete_enquanto_o_destino_esta_em_uso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Windows, ``os.replace`` sobre um arquivo aberto pela API dá PermissionError."""
    monkeypatch.setattr("observatorio.storage.time.sleep", lambda _s: None)
    original = Path.replace
    falhas = iter([True, True, False])

    def ocupado(self: Path, target: Path) -> Path:
        if next(falhas):
            raise PermissionError(13, "Acesso negado")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", ocupado)
    destino = tmp_path / "x.txt"

    atomic_write_text(destino, "novo")

    assert destino.read_text(encoding="utf-8") == "novo"


def test_substituicao_desiste_depois_das_tentativas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    esperas: list[float] = []
    monkeypatch.setattr("observatorio.storage.time.sleep", esperas.append)

    def sempre_ocupado(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Acesso negado")

    monkeypatch.setattr(Path, "replace", sempre_ocupado)

    with pytest.raises(PermissionError), storage.atomic_path(tmp_path / "y.parquet") as tmp:
        tmp.write_bytes(b"x")

    assert len(esperas) == storage.REPLACE_ATTEMPTS - 1
    assert max(esperas) == storage.REPLACE_WAIT_S[1]
    assert list(tmp_path.iterdir()) == []  # o temporário não fica para trás


def test_gzip_copy_grava_ao_lado_e_descomprime_igual(tmp_path: Path) -> None:
    origem = tmp_path / "malha.geojson"
    origem.write_bytes(b'{"type": "FeatureCollection", "features": []}' * 1000)

    destino = storage.gzip_copy(origem)

    assert destino == tmp_path / "malha.geojson.gz"
    assert gzip.decompress(destino.read_bytes()) == origem.read_bytes()
    assert destino.stat().st_size < origem.stat().st_size
    assert sorted(p.name for p in tmp_path.iterdir()) == ["malha.geojson", "malha.geojson.gz"]


def test_copy_query_aceita_caminho_com_aspas(tmp_path: Path) -> None:
    destino = tmp_path / "d'água" / "x.parquet"
    con = connect()
    try:
        storage.copy_query(con, "SELECT 1 AS a", destino)
        assert con.execute(f"SELECT a FROM read_parquet({sql_path(destino)})").fetchall() == [(1,)]
    finally:
        con.close()

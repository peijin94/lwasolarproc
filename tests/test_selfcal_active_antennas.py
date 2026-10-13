"""Small table-reader fixtures; real casacore/MS is exercised on calimsolar."""
import sys
from types import ModuleType

import numpy as np
import pytest

from lwasolarproc import preprocessing_and_imaging as pipeline


def test_active_antennas_are_derived_from_usable_cross_correlations(monkeypatch, tmp_path):
    data = {
        "ANTENNA1": np.array([0, 1, 2, 3, 4, 5]),
        "ANTENNA2": np.array([1, 2, 3, 4, 4, 5]),
        "FLAG": np.array([False, True, False, False, False, False])[:, None, None],
        "FLAG_ROW": np.array([False, False, True, False, False, False]),
        "DATA": np.ones((6, 1, 1), dtype=complex),
        "WEIGHT_SPECTRUM": np.array([1, 1, 1, 0, 1, 1])[:, None, None],
    }

    class Table:
        def __init__(self, antenna=False): self.antenna = antenna
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def colnames(self): return list(data)
        def nrows(self): return 6
        def getcol(self, col, startrow=0, nrow=-1):
            if self.antenna: return [f"ant{i}" for i in range(6)]
            values = data[col]
            return values[startrow:] if nrow < 0 else values[startrow:startrow+nrow]

    fake = ModuleType("casacore.tables")
    fake.table = lambda name, **kw: Table(str(name).endswith("ANTENNA"))
    monkeypatch.setitem(sys.modules, "casacore.tables", fake)
    assert pipeline.phase_selfcal_active_antennas(tmp_path / "input.ms", chunk_rows=2) == ["ant0", "ant1"]

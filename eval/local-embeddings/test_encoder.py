import os
import socket
import unittest
from pathlib import Path

import numpy as np
from encoder import Encoder, normalized, spans


def forbidden(*args, **kwargs):
    raise AssertionError("Network forbidden")


socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden


class GeometryTests(unittest.TestCase):
    def test_all_token_positions_including_tail(self):
        for capacity in (240, 254, 500, 510):
            for length in (1, capacity - 1, capacity, capacity + 1, 2100, 30000):
                windows = spans(length, capacity)
                self.assertEqual(windows[0][0], 0)
                self.assertEqual(windows[-1][1], length)
                covered = set()
                for start, end in windows:
                    self.assertLessEqual(end - start, capacity)
                    covered.update(range(start, end))
                self.assertEqual(covered, set(range(length)))
        with self.assertRaises(ValueError):
            spans(9999999, 254)

    def test_invalid_vectors(self):
        for value in ([0, 0], [np.nan, 1], [np.inf, 2]):
            with self.assertRaises(ValueError):
                normalized(np.array(value))

    def test_real_local_models_shapes_normalization_and_padding(self):
        for name in ("bge", "minilm"):
            with self.subTest(model=name):
                encoder = Encoder(Path(os.environ["GCT_STUDY_SPECS"]) / f"{name}-spec.json")
                short = "The cedar observatory closes at six."
                long = "recollection " * 700
                vectors = encoder.encode([short, long])
                self.assertEqual(vectors[0].shape, (1, 384))
                self.assertGreater(len(vectors[1]), 1)
                self.assertEqual(vectors[1].dtype, np.float32)
                np.testing.assert_allclose(np.linalg.norm(vectors[1], axis=1), 1, atol=1e-5)
                self.assertEqual(encoder.last_stats["truncated_tokens"], 0)
                # Quantized dynamic kernels need not be bit-identical across padding.
                single = encoder.encode([short])[0][0]
                self.assertGreater(float(single @ vectors[0][0]), 0.99)
                query = encoder.query("When does the observatory close?")
                self.assertEqual(query.shape, (384,))
                self.assertAlmostEqual(float(np.linalg.norm(query)), 1, places=5)
                with self.assertRaises(ValueError):
                    encoder.encode([" "])


if __name__ == "__main__":
    unittest.main()

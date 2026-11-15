import unittest
from types import SimpleNamespace
from dali_mcp.session_gate import SessionGate


class GateTests(unittest.TestCase):
    def test_inflight_call_prevents_switch_and_exception_releases(self):
        gate=SessionGate()
        with self.assertRaisesRegex(ValueError,'call failure'):
            with gate.operation():
                with self.assertRaises(RuntimeError):
                    with gate.selection([]):self.fail('must not switch')
                raise ValueError('call failure')
        with gate.selection([]):self.assertTrue(gate.switching)

    def test_background_active_or_closing_thread_prevents_switch(self):
        gate=SessionGate()
        for controller in [SimpleNamespace(active='job'),SimpleNamespace(active={'blink':True}),
                           SimpleNamespace(active=None,worker=SimpleNamespace(is_alive=lambda:True))]:
            with self.assertRaises(RuntimeError):
                with gate.selection([controller]):self.fail('must not switch')
        with gate.selection([SimpleNamespace(active=None,worker=SimpleNamespace(is_alive=lambda:False))]):pass

    def test_selection_excludes_calls_and_other_selections(self):
        gate=SessionGate()
        with self.assertRaises(ValueError):
            with gate.selection([]):
                with self.assertRaises(RuntimeError):
                    with gate.operation():self.fail('must not start')
                with self.assertRaises(RuntimeError):
                    with gate.selection([]):self.fail('must not select')
                raise ValueError('connect failed')
        with gate.operation():self.assertEqual(gate.calls,1)


if __name__=='__main__':unittest.main(verbosity=2)
